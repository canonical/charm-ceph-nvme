#!/usr/bin/env python3

"""Charm the application."""

import json
import logging
import os
import random
import socket
import subprocess

import interface_ceph_client.ceph_client as ceph_client
import ops

import utils

logger = logging.getLogger(__name__)

SPDK_TGT_FILE = '/lib/systemd/system/nvmf_tgt.service'
PROXY_FILE = '/lib/systemd/system/nvmf_proxy.service'
PROXY_CMDS_FILE = '/var/lib/nvme-of/cmds.json'

SYSTEMD_TEMPLATE = """
[Unit]
Description={description}
[Service]
User=root
ExecStart={path} {args}
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
"""


class CephNVMECharm(ops.CharmBase):
    """Charm the application."""

    PACKAGES = ['librados-dev', 'librbd-dev']

    CAPABILITIES = [
        "osd", "allow *",
        "mon", "allow *",
        "mgr", "allow r"]

    def __init__(self, *args):
        super().__init__(*args)
        self.client = ceph_client.CephClientRequires(self, 'ceph-client')
        self.rpc = utils.RPC()
        obs = self.framework.observe
        obs(self.on.start, self._on_start)
        obs(self.on.install, self._on_install)
        obs(self.on.create_endpoint_action, self._on_create_endpoint_action)
        obs(self.on.delete_endpoint_action, self._on_delete_endpoint_action)
        obs(self.on.join_endpoint_action, self._on_join_endpoint_action)
        obs(self.on.leave_endpoint_action, self._on_leave_endpoint_action)
        obs(self.on.list_endpoints_action, self._on_list_endpoints_action)
        obs(self.client.on.broker_available, self._on_ceph_relation_joined)
        obs(self.client.on.pools_available, self._on_ceph_relation_changed)

    def bind_addr(self):
        """Get the charm's local binding address."""
        try:
            addr = self.model.get_binding('peers').network.bind_address
            return str(addr)
        except Exception:
            return '0.0.0.0'

    def egress_addr(self):
        """Get the charm's egress address."""
        try:
            net = self.model.get_binding('peers').network.egress_subnets
            elem = next(iter(net))
            return str(list(elem.hosts())[0])
        except Exception:
            return '0.0.0.0'

    def _rpc_sock(self):
        """Create a socket to communicate with the proxy."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('0.0.0.0', 0))
        sock.settimeout(0.3)
        return sock

    def _msgloop(self, msg, addr=None, sock=None):
        """Send an RPC message and receive a response."""
        orig_sock = sock
        if sock is None:
            sock = self._rpc_sock()

        binmsg = json.dumps(msg).encode('utf8')
        if addr is None:
            addr = self.bind_addr()
        sock.sendto(binmsg, (addr, self.config['proxy-port']))

        try:
            return json.loads(sock.recv(4096))
        finally:
            if orig_sock is None:
                sock.close()

    @property
    def app_name(self):
        return self.model.unit.app.name

    def _on_ceph_relation_joined(self, event):
        self.client.request_ceph_permissions(
            self.app_name, self.CAPABILITIES)

    def _on_ceph_relation_changed(self, event):
        """Handle the Ceph relation."""
        data = self.client.get_relation_data()
        if not data:
            logger.warning('no Ceph relation data found - skipping')
            return

        relation = self.model.relations.get(self.client.relation_name)

        msg = self.rpc.cluster_add(
            user=self.app_name, key=data['key'],
            mon_host=','.join(data['mon_hosts']),
            name='ceph.%s' % next(iter(relation)).id)
        res = self._msgloop(msg)
        if 'error' in res and res['error']['code'] != -1:
            # Cluster creation failed and not because it already exists.
            err = str(res['error'])
            logging.error('failed to create cluster: %s' % err)
            event.fail('error creating cluster: %s' % err)

    @staticmethod
    def _get_unit_addr(unit, rel_id):
        try:
            cmd = ['relation-get', '--format=json', '-r', str(rel_id),
                   'private-address', unit]
            out = subprocess.check_output(cmd)
            return out.decode('utf8').replace('"', '').strip()
        except subprocess.CalledProcessError:
            logger.exception('failed to get private address: ')
            return None

    def _get_peers(self):
        peers = self.model.relations.get('peers')
        if peers:
            peers = next(iter(peers))
        return peers

    def _select_ha_units(self, unit_spec):
        """Given a spec, pick a proper subset of peers to handle HA."""
        peers = self._get_peers()
        units = []

        if not peers:
            return units

        try:
            # If an integer is provided, select that many random units.
            cnt = int(unit_spec)
            if cnt <= 1:
                return units

            peer_units = list(peers.units - set((self.model.unit,)))
            units = random.sample(peer_units, min(cnt, len(peer_units)))
        except ValueError:
            # Select the specified units from the list.
            unit_names = unit_spec.split(',')
            units = [unit for unit in peers.units if unit.name in unit_names]

        ret = []
        for unit in units:
            addr = self._get_unit_addr(unit.name, peers.id)
            if addr is not None:
                ret.append((unit.name, addr))

        return ret

    @staticmethod
    def _exclude(lst, idx):
        return lst[0:idx] + lst[idx + 1:]

    @staticmethod
    def _event_set_create_results(event, response, units):
        event.set_results({'nqn': response['nqn'],
                           'address': response['addr'],
                           'port': response['port'],
                           'units': units})

    def _handle_ha_create(self, response, peers, msg, event):
        sock = self._rpc_sock()
        valid = [{'addr': self.egress_addr(), 'port': response['port'],
                  'rpc_addr': '127.0.0.1'}]

        # Tell the other peers to create the bdev, subsystem and namespace.
        for peer, addr in peers:
            peer_resp = self._msgloop(msg, addr=addr, sock=sock)
            if 'error' not in peer_resp:
                valid.append({'addr': peer_resp['addr'], 'rpc_addr': addr,
                              'port': peer_resp['port']})

        # Now make each peer refer to the others.
        added = 0
        if len(valid) > 1:
            for i, peer in enumerate(valid):
                join_msg = self.rpc.join(nqn=response['nqn'],
                                         addresses=self._exclude(valid, i))
                peer_resp = self._msgloop(join_msg, addr=peer['rpc_addr'],
                                          sock=sock)
                if 'error' not in peer_resp:
                    added += 1

        if not added:
            logger.warning('failed to create additional endpoints for HA')
        self._event_set_create_results(event, response, added)

    def _on_create_endpoint_action(self, event):
        """Handle endpoint creation."""
        relations = self.model.relations.get(self.client.relation_name)
        if not relations:
            event.fail('no Ceph relation found')
            return

        pool = event.params.get('rbd-pool')
        image = event.params.get('rbd-image')
        cluster = 'ceph.%s' % next(iter(relations)).id
        units = event.params.get('units') or "1"

        msg = self.rpc.create(pool_name=pool, rbd_name=image, cluster=cluster)
        res = self._msgloop(msg)
        if 'error' in res:
            event.fail('failed to create endpoint: %s' % str(res['error']))
            return

        peers = self._select_ha_units(units)
        if not peers:
            if units != "1" or units != self.model.unit.name:
                logger.warning('could not get enough units for HA - '
                               'try running the "join-endpoint" action on '
                               'additional units')
            self._event_set_create_results(event, res, 1)
            return

        # The initial unit allocates the NQN on the fly, while the rest
        # of the peers need to have it set.
        msg['params']['nqn'] = res['nqn']
        self._handle_ha_create(res, peers, msg, event)

    def _leave_endpoint(self, sock, nqn):
        elem = self._msgloop(self.rpc.find(nqn=nqn), sock=sock)
        if not elem:
            return False

        peers = self._get_peers()
        msg = self.rpc.leave(addr=elem['addr'], port=elem['port'], nqn=nqn)
        for peer in peers:
            addr = self._get_unit_addr(peer.name, peers.id)
            if addr is not None:
                # We don't care about the response here.
                self._msgloop(msg, addr=addr, sock=sock)

        return True

    def _on_delete_endpoint_action(self, event):
        """Handle endpoint deletion."""
        nqn = event.params.get('nqn')
        sock = self._rpc_sock()
        res = self._msgloop(self.rpc.remove(nqn=nqn), sock=sock)
        if 'error' in res:
            event.fail('failed to remove endpoint: %s' % str(res['error']))
            return

        self._leave_endpoint(sock, nqn)
        event.set_results({'message': 'success'})

    def _handle_join_peers(self, msg, nqn, peers, num_max, event):
        sock = self._rpc_sock()
        peer_id = peers.id
        bdev_spec = None
        joined = 0

        for peer in peers.units:
            addr = self._get_unit_addr(peer.name, peer_id)
            if addr is None:
                continue

            resp = self._msgloop(msg, addr=addr, sock=sock)
            if not resp:
                # Empty response means this peer doesn't handle the NQN.
                continue

            if bdev_spec is None:
                # Create the endpoint if we haven't already.
                create_msg = self.rpc.create(nqn=nqn, pool_name=resp['pool'],
                                             rbd_name=resp['image'],
                                             cluster=resp['cluster'])
                rv = self._msgloop(create_msg, addr='127.0.0.1', sock=sock)
                if 'error' in rv:
                    event.fail('failed to create endpoint: %s' %
                               str(rv['error']))
                    return 0, ""

                bdev_spec = rv

            # Tell our peer to join us.
            alist = [{'addr': bdev_spec['addr'], 'port': bdev_spec['port']}]
            join_msg = self.rpc.join(nqn=nqn, addresses=alist)
            rv = self._msgloop(join_msg, addr=addr, sock=sock)
            if 'error' in rv:
                continue

            # On success, join our peer.
            alist[0] = {'addr': addr, 'port': resp['port']}
            rv = self._msgloop(join_msg, addr='127.0.0.1', sock=sock)
            if 'error' in rv:
                leave_msg = self.rpc.leave(
                    nqn=nqn, addr=bdev_spec['addr'], port=bdev_spec['port'])
                self._msgloop(leave_msg, addr=addr, sock=sock)
            else:
                joined += 1
                if joined >= num_max:
                    break

        return joined, bdev_spec

    def _on_join_endpoint_action(self, event):
        """Join an endpoint."""
        peers = self._get_peers()
        if not peers:
            event.fail('no peers to join')
            return

        nqn = event.params.get('nqn')
        num_max = event.params.get('nmax', -1)
        if num_max <= 0:
            num_max = len(peers.units)

        msg = self.rpc.find(nqn=nqn)
        joined, bdev = self._handle_join_peers(msg, nqn, peers, num_max, event)

        if not joined:
            if bdev is None:
                event.fail('NQN not found')
                return
            logger.warning('endpoint created but could not join any units')
        self._event_set_create_results(event, bdev, joined)

    def _on_leave_endpoint_action(self, event):
        """Leave an endpoint."""
        nqn = event.params.get('nqn')
        sock = self._rpc_sock()
        if self._leave_endpoint(sock, nqn):
            event.set_results({'message': 'success'})
        else:
            event.fail('NQN not found in this unit')

    def _on_list_endpoints_action(self, event):
        elems = self._msgloop({'method': 'list'})
        event.set_results({'endpoints': elems})

    def _install_packages(self, packages):
        # Code taken from charmhelpers.
        cmd = ['sudo', 'apt-get', '--assume-yes',
               '--option=Dpkg::Options::=--force-confold',
               'install'] + packages
        subprocess.check_call(cmd)

    def _install_systemd_services(self, cpumask):
        self._install_packages(self.PACKAGES)
        charm_dir = os.environ.get('JUJU_CHARM_DIR', './')
        nvmf_path = os.path.realpath(charm_dir + '/spdk/build/bin/nvmf_tgt')
        nvmf_tgt = os.path.realpath(charm_dir + '/src/nvmf.py')
        utils.create_systemd_svc(SPDK_TGT_FILE, SYSTEMD_TEMPLATE,
                                 description='NVMe-oF target',
                                 path=nvmf_tgt,
                                 args=' '.join([nvmf_path, '-m', cpumask]))

        proxy_path = os.path.realpath(charm_dir + '/src/proxy.py')
        args = ' '.join([str(self.config['proxy-port']),
                         PROXY_CMDS_FILE, self.egress_addr()])

        utils.create_systemd_svc(PROXY_FILE, SYSTEMD_TEMPLATE,
                                 description='proxy server for target',
                                 path=proxy_path, args=args)

    def _on_install(self, event):
        """Handle charm installation."""
        utils.create_dir(os.path.dirname(PROXY_CMDS_FILE))
        cpuset = self.config.get("cpuset", "")
        cpumask = utils.compute_cpumask(utils.compute_cpuset(cpuset))
        self._install_systemd_services(hex(cpumask))

    def _on_start(self, event: ops.StartEvent):
        """Handle start event."""
        self.unit.status = ops.ActiveStatus('ready')


if __name__ == "__main__":
    ops.main(CephNVMECharm)
