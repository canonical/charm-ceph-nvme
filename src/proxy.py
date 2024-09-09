#! /usr/bin/env python3
#
# Copyright 2024 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import json
import logging
import os
import pickle
import socket
import sys
import time
import uuid

sys.path.append(os.path.dirname(os.path.abspath(__name__)))
import utils


NQN_BASE = 'nqn.2014-08.org.nvmexpress:uuid:'

logger = logging.getLogger(__name__)


def _json_dumps(x):
    return json.dumps(x, separators=(',', ':'))


class ProxyError(Exception):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class ProxyCommand:
    def __init__(self, msg, fatal=False):
        self.msg = msg
        self.fatal = fatal

    def __call__(self, proxy):
        return proxy.msgloop(self.msg)


class ProxyCreateEndpoint:
    def __init__(self, msg, bdev_name, cluster):
        self.msg = msg
        self.bdev_name = bdev_name
        self.cluster = cluster

    @staticmethod
    def _check_reply(msg, proxy):
        reply = proxy.msgloop(msg)
        if proxy.is_error(reply):
            raise ValueError('%s failed: %s' % (msg['method'], reply))
        return reply

    def _add_listener(self, proxy, cleanup, **kwargs):
        params = kwargs['listen_address']
        port, adrfam = utils.get_free_port(params['traddr'])
        params['adrfam'] = str(adrfam)
        params['trsvcid'] = str(port)
        payload = proxy.rpc.nvmf_subsystem_add_listener(**kwargs)
        self._check_reply(payload, proxy)
        cleanup.append(proxy.rpc.nvmf_subsystem_remove_listener(**kwargs))

    def __call__(self, proxy):
        cleanup = []
        rpc = proxy.rpc
        nqn = self.msg['nqn']
        try:
            payload = rpc.bdev_rbd_create(
                name=self.bdev_name, pool_name=self.msg['pool_name'],
                rbd_name=self.msg['rbd_name'],
                cluster_name=self.cluster, block_size=4096)
            self._check_reply(payload, proxy)
            cleanup.append(rpc.bdev_rbd_delete(name=self.bdev_name))

            payload = rpc.nvmf_create_subsystem(
                nqn=nqn, ana_reporting=True, max_namespaces=2)
            self._check_reply(payload, proxy)
            cleanup.append(rpc.nvmf_delete_subsystem(nqn=nqn))

            self._add_listener(
                proxy, cleanup,
                nqn=nqn,
                listen_address=dict(trtype='tcp', traddr=self.msg['addr']))

            payload = rpc.nvmf_subsystem_add_ns(
                nqn=nqn,
                namespace=proxy.ns_dict(self.bdev_name, nqn))
            return self._check_reply(payload, proxy)
        except Exception:
            for call in reversed(cleanup):
                proxy.msgloop(call)
            raise


class ProxyAddHost:
    def __init__(self, msg, dhchap_key):
        self.msg = msg
        self.dhchap_key = dhchap_key

    def __call__(self, proxy):
        if not self.dhchap_key:
            return proxy.msgloop(self.msg)

        params = self.msg['params']
        fname = proxy.key_file_name(params['nqn'], params['host'])
        path = os.path.join(proxy.wdir, fname)

        try:
            f = open(path, 'r')
            contents = f.read()
            f.close()
        except Exception:
            contents = None

        if contents is not None:
            if self.dhchap_key != contents:
                raise ProxyError('host already present with a different key')
        else:
            with open(path, 'w') as file:
                file.write(self.dhchap_key)

            payload = proxy.rpc.keyring_file_add_key(name=fname, path=path)
            rv = proxy.msgloop(payload)
            if proxy.is_error(rv):
                os.remove(path)
                raise ProxyError(rv['error'])

        payload = self.msg.copy()
        payload['params']['dhchap_key'] = fname
        rv = proxy.msgloop(payload)
        if proxy.is_error(rv) and contents is None:
            proxy.msgloop(proxy.rpc.keyring_file_remove_key(name=fname))
            os.remove(path)

        return rv


class ProxyRemoveHost:
    def __init__(self, msg):
        self.msg = msg

    def __call__(self, proxy):
        nqn, host = self.msg['nqn'], self.msg['host']
        payload = proxy.rpc.nvmf_subsystem_remove_host(nqn=nqn, host=host)
        rv = proxy.msgloop(payload)
        if proxy.is_error(rv):
            return rv

        fname = proxy.key_file_name(nqn, host)
        payload = proxy.rpc.keyring_file_remove_key(name=fname)
        if not proxy.is_error(proxy.msgloop(payload)):
            try:
                os.remove(os.path.join(proxy.wdir, fname))
            except FileNotFoundError:
                pass

        return rv


class Proxy:
    def __init__(self, config_path, rpc_path):
        with open(config_path) as file:
            config = json.loads(file.read())

        self.receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.receiver.bind(('0.0.0.0', config['proxy-port']))
        self.rpc_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._connect(rpc_path)
        self.wdir = os.path.dirname(config_path)
        self.cmd_file = open(os.path.join(self.wdir, 'cmds'), 'a+b')
        self.buffer = bytearray(4096 * 10)
        self.rpc = utils.RPC()

        cmds = iter(self._prepare_file())
        try:
            self._process_cmd(next(cmds))
        except ProxyError:
            # The first command is always 'nvmf_create_transport'
            # Since we're using TCP and support for it is always
            # built in, this can only fail in case the command has
            # already been applied, which can happen if the proxy
            # dies, but not SPDK. As such, assume that SPDK is
            # healthy and needs no further configuring.
            return

        for cmd in cmds:
            try:
                self._process_cmd(cmd)
            except Exception:
                # Check if the failure is fatal.
                if not getattr(cmd, 'fatal', True):
                    continue
                raise

    def _connect(self, rpc_path, timeout=5 * 60):
        # It may take a while for SPDK to come up, specially if
        # we're allocating huge pages, so retry the connection
        # a bit to make up for that.
        end = time.time() + timeout
        while True:
            if os.access(rpc_path, os.F_OK) or time.time() > end:
                self.rpc_sock.connect(rpc_path)
                return

            time.sleep(0.1)

    def get_spdk_subsystems(self):
        """Return a dictionary describing the subsystems for the gateway."""
        obj = self.msgloop(self.rpc.nvmf_get_subsystems())
        if not isinstance(obj, dict):
            logger.warning('did not receive a dict from SPDK: %s', obj)
            return

        obj = obj.get('result', ())
        ret = {}
        for elem in obj:
            nqn = elem.pop('nqn')
            if nqn is None or 'discovery' in nqn:
                continue

            ret[nqn] = elem

        return ret

    def _write_cmd(self, cmd):
        pickle.dump(cmd, self.cmd_file)
        self.cmd_file.flush()

    def _process_cmd(self, cmd):
        obj = cmd(self)
        if not isinstance(obj, dict):
            logger.error('invalid response received (%s - %s)',
                         type(obj), obj)
            raise TypeError()
        elif 'error' in obj:
            logger.error('error running command: %s', obj)
            raise ProxyError(obj['error'])

        return obj

    def _prepare_file(self):
        """Read the contents of the bootstrap file or set it up it if empty."""
        size = self.cmd_file.tell()
        if not size:
            # File is empty.
            logger.info('SPDK file is empty; starting bootstrap process')
            payload = self.rpc.nvmf_create_transport(trtype='tcp')
            cmd = ProxyCommand(payload)
            self._write_cmd(cmd)
            yield cmd
        else:
            self.cmd_file.seek(0)
            while True:
                try:
                    yield pickle.load(self.cmd_file)
                except EOFError:
                    break

    @staticmethod
    def is_error(msg):
        return not isinstance(msg, dict) or 'error' in msg

    def msgloop(self, msg):
        """Send an RPC to SPDK and receive the response."""
        self.rpc_sock.sendall(json.dumps(msg).encode('utf8'))
        nbytes = self.rpc_sock.recv_into(self.buffer)
        try:
            return json.loads(self.buffer[:nbytes])
        except Exception:
            return None

    def _get_method_handlers(self, method):
        expand = getattr(self, '_expand_' + method, None)
        post = getattr(self, '_post_' + method, None)

        if expand is None and post is not None:
            expand = lambda *_: ()   # noqa

        return expand, post

    def handle_request(self, msg, addr):
        """Handle a request from a particular client."""
        obj = json.loads(msg)
        method = obj['method'].strip()
        if method == 'stop':
            logger.debug('stopping proxy as requested')
            return True

        handler, post = self._get_method_handlers(method)
        if handler is None:
            logger.error('invalid method: %s', method)
            self.receiver.sendto(('{"error": "invalid method: %s"}' %
                                 method).encode('utf8'), addr)
            return

        logger.info('processing request: %s', obj)
        obj = obj.get('params')
        cmds = list(handler(obj))
        for cmd in cmds:
            self._process_cmd(cmd)

        # Only write the commands after they've succeeded.
        for cmd in cmds:
            self._write_cmd(cmd)

        resp = {}
        if post is not None:
            resp = post(obj)
        self.receiver.sendto(_json_dumps(resp).encode('utf8'), addr)

    @staticmethod
    def _make_exc_msg(exc):
        if isinstance(exc, ProxyError):
            return exc.args[0]

        return {"code": -2, "type": str(type(exc)), "message": str(exc)}

    def serve(self):
        """Main server loop."""
        while True:
            inaddr = None
            try:
                nbytes, inaddr = self.receiver.recvfrom_into(self.buffer)
                logger.info('got a request from address ', inaddr)
                rv = self.handle_request(self.buffer[:nbytes], inaddr)
                if rv:
                    logger.warning('got a request to stop proxy')
                    return
            except Exception as exc:
                logger.exception('caught exception: ')
                if inaddr is not None:
                    err = {"error": self._make_exc_msg(exc)}
                    self.receiver.sendto(json.dumps(err).encode('utf8'),
                                         inaddr)

    # RPC handlers.

    @staticmethod
    def _parse_bdev_name(name):
        ix = name.find('://')
        ret = json.loads(name[ix + 3:])
        ret['type'] = name[:ix]
        return ret

    @staticmethod
    def key_file_name(nqn, host):
        # Create a unique file path for a key.
        nqn = nqn.replace(NQN_BASE, '').replace('-', '')
        return nqn + '@' + host

    @staticmethod
    def ns_dict(bdev_name, nqn):
        # In order for namespaces to be equal, the following must match:
        # namespace ID (always set to 1)
        # NGUID (32 bytes)
        # EUI64 (16 bytes)
        # UUID
        # The latter 3 are derived from the NQN, which is either allocated
        # on the fly, or passed in as a parameter.
        uuid = nqn[len(NQN_BASE):]
        base = uuid.replace('-', '')
        return dict(bdev_name=bdev_name, nsid=1, nguid=base,
                    eui64=base[:16], uuid=uuid)

    @staticmethod
    def _subsystem_to_dict(subsys):
        elem = subsys['listen_addresses'][0]
        return {'addr': elem['traddr'], 'port': elem['trsvcid'],
                'hosts': subsys['hosts'],
                'allow_any_host': subsys['allow_any_host'],
                **Proxy._parse_bdev_name(subsys['namespaces'][0]['name'])}

    def _expand_create(self, msg):
        cluster = msg['cluster']
        bdev = {'pool': msg['pool_name'], 'image': msg['rbd_name'],
                'cluster': cluster}
        bdev_name = 'rbd://' + _json_dumps(bdev)

        nqn = msg.get('nqn')
        if nqn is None:
            nqn = NQN_BASE + str(uuid.uuid4())
            msg['nqn'] = nqn   # Inject it to use it in the post handler.

        yield ProxyCreateEndpoint(msg, bdev_name, cluster)

    def _post_create(self, msg):
        subsystems = self.get_spdk_subsystems()
        nqn = msg['nqn']
        sub = subsystems[nqn]
        trid = sub['listen_addresses'][0]
        return {'nqn': nqn, 'addr': trid['traddr'], 'port': trid['trsvcid']}

    def _expand_remove(self, msg):
        nqn = msg['nqn']
        name = self.get_spdk_subsystems()[nqn]['namespaces'][0]['name']
        payload = self.rpc.nvmf_subsystem_remove_ns(
            nqn=msg['nqn'], nsid=1)
        yield ProxyCommand(payload)

        payload = self.rpc.nvmf_delete_subsystem(nqn=msg['nqn'])
        yield ProxyCommand(payload)

        payload = self.rpc.bdev_rbd_delete(name=name)
        yield ProxyCommand(payload)

    def _expand_cluster_add(self, msg):
        payload = self.rpc.bdev_rbd_register_cluster(
            name=msg['name'], user_id=msg['user'],
            config_param={'key': msg['key'], 'mon_host': msg['mon_host']})
        yield ProxyCommand(payload)

    def _expand_join(self, msg):
        nqn = msg['nqn']
        subsystems = self.get_spdk_subsystems()
        if nqn not in subsystems:
            return

        for elem in msg.get('addresses', ()):
            payload = self.rpc.nvmf_discovery_add_referral(
                subnqn=nqn, address=dict(
                    trtype='tcp', traddr=elem['addr'],
                    trsvcid=str(elem['port'])))
            yield ProxyCommand(payload)

    def _post_find(self, msg):
        subsys = self.get_spdk_subsystems()[msg['nqn']]
        return self._subsystem_to_dict(subsys) if subsys else {}

    def _post_list(self, msg):
        subsystems = self.get_spdk_subsystems()
        return [{'nqn': nqn, **self._subsystem_to_dict(subsys)}
                for nqn, subsys in subsystems.items()]

    def _post_host_list(self, msg):
        subsys = self.get_spdk_subsystems().get(msg['nqn'])
        if subsys is None:
            return {'error': 'nqn not found'}
        elif subsys.get('allow_any_host'):
            return 'any'
        return subsys.get('hosts', [])

    def _expand_leave(self, msg):
        elems = msg.get('subsystems')
        if elems is None:
            elems = [msg]

        for subsys in elems:
            payload = self.rpc.nvmf_discovery_remove_referral(
                subnqn=subsys['nqn'],
                address=dict(
                    traddr=subsys['addr'], trsvcid=str(subsys['port']),
                    trtype='tcp'))
            yield ProxyCommand(payload, fatal=False)

    def _expand_host_add(self, msg):
        host = msg['host']
        if host == 'any':
            payload = self.rpc.nvmf_subsystem_allow_any_host(
                nqn=msg['nqn'], allow_any_host=True)
            yield ProxyCommand(payload)
        else:
            payload = self.rpc.nvmf_subsystem_add_host(
                nqn=msg['nqn'], host=host)
            yield ProxyAddHost(payload, msg.get('dhchap_key'))

    def _expand_host_del(self, msg):
        yield ProxyRemoveHost(msg)


def main():
    parser = argparse.ArgumentParser(description='proxy server for SPDK')
    parser.add_argument('config', help='path to configuration file')
    parser.add_argument('-s', dest='sock', help='local socket for RPC',
                        type=str, default='/var/tmp/spdk.sock')
    args = parser.parse_args()
    proxy = Proxy(args.config, args.sock)
    proxy.serve()


if __name__ == '__main__':
    main()
