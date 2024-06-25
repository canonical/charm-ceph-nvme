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
from collections import namedtuple
import json
import logging
import os
import socket
import sys
import uuid

sys.path.append(os.path.dirname(os.path.abspath(__name__)))
import utils


NQN_BASE = 'nqn.2014-08.org.nvmexpress:uuid:'
RPCHandler = namedtuple('RPCHandler', ['expand', 'post'])

logger = logging.getLogger(__name__)


def _json_dumps(x):
    return json.dumps(x, separators=(',', ':'))


def _dump_and_append(out, payload):
    out.append(_json_dumps(payload) + '\n')


class ProxyError(Exception):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class Proxy:
    def __init__(self, port, cmd_file, xaddr, rpc_path):
        self.rpc_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.rpc_sock.connect(rpc_path)
        self.receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.receiver.bind(('0.0.0.0', port))
        self.cmd_file = open(cmd_file, 'a+')
        self.buffer = bytearray(4096 * 10)
        self.rpc = utils.RPC()
        self.xaddr = xaddr
        self.handlers = {
            'create': RPCHandler(self._expand_create, self._post_create),
            'remove': RPCHandler(self._expand_remove, None),
            'cluster_add': RPCHandler(self._expand_cluster_add, None),
            'join': RPCHandler(self._expand_join, None),
            'find': RPCHandler(self._expand_default, self._post_find),
            'leave': RPCHandler(self._expand_leave, None),
            'list': RPCHandler(self._expand_default, self._post_list),
        }

        for line in self._prepare_file():
            self._process_line(line)

    def fetch_spdk_state(self):
        """Return a dictionary describing the subsystems for the gateway."""
        msg = self.rpc.nvmf_get_subsystems()
        obj = self._receive_response(json.dumps(msg).encode('utf8'))
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

    def _prepare_file(self):
        """Read the contents of the bootstrap file or set it up it if empty."""
        self.cmd_file.seek(0)
        contents = self.cmd_file.read().strip()
        if not contents:
            # File is empty.
            logger.info('SPDK file is empty; starting bootstrap process')
            payload = self.rpc.nvmf_create_transport(trtype='tcp')
            contents = _json_dumps(payload) + '\n'
            self.cmd_file.write(contents)
            self.cmd_file.flush()

        return contents.split('\n')

    def _expand_line(self, line):
        """Fill in the placeholders in a command."""
        xaddr, xport, adrfam = self.xaddr, '0', ''
        if '{xport}' in line:
            xport, adrfam = utils.get_free_port(self.xaddr)

        for elem in (('{xport}', xport), ('{xaddr}', xaddr),
                     ('{adrfam}', adrfam)):
            line = line.replace(elem[0], str(elem[1]))

        return line

    def _receive_response(self, msg):
        """Send an RPC to SPDK and receive the response."""
        self.rpc_sock.sendall(msg)
        nbytes = self.rpc_sock.recv_into(self.buffer)
        try:
            return json.loads(self.buffer[:nbytes])
        except Exception:
            return None

    def handle_request(self, msg, addr):
        """Handle a request from a particular client."""
        obj = json.loads(msg)
        method = obj['method'].strip()
        if method == 'stop':
            logger.debug('stopping proxy as requested')
            return True

        handler = self.handlers.get(method)
        if handler is None:
            logger.error('invalid method: %s', method)
            self.receiver.sendto(('{"error": "invalid method: %s"}' %
                                 method).encode('utf8'), addr)
            return

        logger.info('processing request: %s', obj)
        obj = obj.get('params')
        lines = handler.expand(obj)
        for line in lines:
            self._process_line(line)
            self.cmd_file.write(line)
            self.cmd_file.write('\n')
            self.cmd_file.flush()

        resp = {}
        if handler.post is not None:
            resp = handler.post(obj)
        self.receiver.sendto(_json_dumps(resp).encode('utf8'), addr)

    def _process_line(self, line):
        """Process a single command (or line)."""
        if not line:
            return

        msg = self._expand_line(line).encode('utf8')
        obj = self._receive_response(msg)

        if not isinstance(obj, dict):
            logger.error('invalid response received (%s - %s)',
                         type(obj), obj)
            raise TypeError()
        elif 'error' in obj:
            logger.error('error running command: %s', obj)
            raise ProxyError(obj['error'])

        return obj

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
    def _ns_dict(bdev_name, nqn):
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
                **Proxy._parse_bdev_name(subsys['namespaces'][0]['name'])}

    def _expand_default(self, _):
        return []

    def _expand_create(self, msg):
        cluster = msg['cluster']
        bdev = {'pool': msg['pool_name'], 'image': msg['rbd_name'],
                'cluster': cluster}
        bdev_name = 'rbd://' + _json_dumps(bdev)

        nqn = msg.get('nqn')
        if nqn is None:
            nqn = NQN_BASE + str(uuid.uuid4())
            msg['nqn'] = nqn   # Inject it to use it in the post handler.

        ret = []

        payload = self.rpc.bdev_rbd_create(
            name=bdev_name, pool_name=msg['pool_name'],
            rbd_name=msg['rbd_name'],
            cluster_name=cluster, block_size=4096)
        _dump_and_append(ret, payload)

        payload = self.rpc.nvmf_create_subsystem(
            nqn=nqn, ana_reporting=True, max_namespaces=2,
            allow_any_host=True   # XXX: Remove when ready.
        )
        _dump_and_append(ret, payload)

        payload = self.rpc.nvmf_subsystem_add_listener(
            nqn=nqn,
            listen_address=dict(trtype='tcp', traddr='{xaddr}',
                                adrfam='{adrfam}', trsvcid='{xport}'))
        _dump_and_append(ret, payload)

        payload = self.rpc.nvmf_subsystem_add_ns(
            nqn=nqn,
            namespace=self._ns_dict(bdev_name, nqn))
        _dump_and_append(ret, payload)
        return ret

    def _post_create(self, msg):
        subsystems = self.fetch_spdk_state()
        nqn = msg['nqn']
        sub = subsystems[nqn]
        lst = sub['listen_addresses'][0]
        return {'addr': lst['traddr'], 'nqn': nqn, 'port': lst['trsvcid']}

    def _expand_remove(self, msg):
        ret = []
        payload = self.rpc.nvmf_subsystem_remove_ns(
            nqn=msg['nqn'], nsid=1)
        _dump_and_append(ret, payload)

        payload = self.rpc.nvmf_delete_subsystem(nqn=msg['nqn'])
        _dump_and_append(ret, payload)

        return ret

    def _expand_cluster_add(self, msg):
        payload = self.rpc.bdev_rbd_register_cluster(
            name=msg['name'], user_id=msg['user'],
            config_param={'key': msg['key'], 'mon_host': msg['mon_host']})
        return [_json_dumps(payload) + '\n']

    def _expand_join(self, msg):
        nqn = msg['nqn']
        subsystems = self.fetch_spdk_state()
        if nqn not in subsystems:
            return []

        ret = []
        for elem in msg.get('addresses', ()):
            payload = self.rpc.nvmf_discovery_add_referral(
                subnqn=nqn, address=dict(
                    trtype='tcp', traddr=elem['addr'],
                    trsvcid=str(elem['port'])))
            _dump_and_append(ret, payload)

        return ret

    def _post_find(self, msg):
        subsys = self.fetch_spdk_state()[msg['nqn']]
        return self._subsystem_to_dict(subsys) if subsys else {}

    def _post_list(self, msg):
        subsystems = self.fetch_spdk_state()
        return [{'nqn': nqn, **self._subsystem_to_dict(subsys)}
                for nqn, subsys in subsystems.items()]

    def _expand_leave(self, msg):
        payload = self.rpc.nvmf_discovery_remove_referral(
            subnqn=msg['nqn'],
            address=dict(
                traddr=msg['addr'], trsvcid=str(msg['port']), trtype='tcp'))
        return [_json_dumps(payload) + '\n']


def main():
    parser = argparse.ArgumentParser(description='proxy server for SPDK')
    parser.add_argument('port', help='proxy server port', type=int)
    parser.add_argument('cmdfile', help='path to file to save commands',
                        type=str, default='/var/lib/nvme-of/cmds.json')
    parser.add_argument('external_addr', help='external address for listeners',
                        type=str, default='0.0.0.0')
    parser.add_argument('-s', dest='sock', help='local socket for RPC',
                        type=str, default='/var/tmp/spdk.sock')
    args = parser.parse_args()
    proxy = Proxy(args.port, args.cmdfile, args.external_addr, args.sock)
    proxy.serve()


if __name__ == '__main__':
    main()
