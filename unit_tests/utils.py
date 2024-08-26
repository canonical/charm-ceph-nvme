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

import json
import select
import socket


class MockSPDK:
    def __init__(self, sock):
        new_sock, _ = sock.accept()
        sock.close()
        self.sock = new_sock
        self.bdevs = {}
        self.clusters = set()
        self.subsystems = {}
        self.referrals = []
        self.handlers = {
            'nvmf_create_transport': self._mock_create_transport,
            'nvmf_get_subsystems': self._mock_get_subsystems,
            'bdev_rbd_register_cluster': self._mock_register_cluster,
            'bdev_rbd_create': self._mock_rbd_create,
            'bdev_rbd_delete': self._mock_rbd_delete,
            'nvmf_create_subsystem': self._mock_create_subsystem,
            'nvmf_subsystem_add_listener': self._mock_add_listener,
            'nvmf_subsystem_add_ns': self._mock_add_ns,
            'nvmf_subsystem_remove_ns': self._mock_remove_ns,
            'nvmf_delete_subsystem': self._mock_delete_subsystem,
            'nvmf_discovery_add_referral': self._mock_add_referral,
            'nvmf_discovery_remove_referral': self._mock_remove_referral,
            'nvmf_subsystem_add_host': self._mock_add_host,
            'nvmf_subsystem_remove_host': self._mock_remove_host,
            'nvmf_subsystem_allow_any_host': self._mock_allow_any_host,
        }

    def _find_subsys(self, nqn):
        return self.subsystems.get(nqn, b'{"error":"subsystem not found"}')

    def _mock_create_transport(self, _):
        pass

    def _mock_register_cluster(self, params):
        name = params['name']
        if name in self.clusters:
            return b'{"error": "cluster already registered"}'
        self.clusters.add(name)

    def _mock_rbd_create(self, params):
        cluster = params['cluster_name']
        if cluster not in self.clusters:
            return b'{"error":"cluster not found"}'
        self.bdevs.setdefault(params['name'],
                              {'pool': params['pool_name'],
                               'image': params['rbd_name'],
                               'cluster': params['cluster_name']})

    def _mock_rbd_delete(self, params):
        del self.bdevs[params['name']]

    def _mock_create_subsystem(self, params):
        dfl = {'listen_addresses': [], 'namespaces': [None],
                'hosts': [(False, None)]}
        self.subsystems.setdefault(params['nqn'], dfl)

    def _mock_add_listener(self, params):
        addr = params['listen_address']
        subsys = self._find_subsys(params['nqn'])
        if isinstance(subsys, bytes):
            return subsys

        params = params.copy()
        del params['listen_address']
        params.update(addr)
        subsys['listen_addresses'].append(params)

    def _mock_add_ns(self, params):
        subsys = self._find_subsys(params['nqn'])
        if isinstance(subsys, bytes):
            return subsys

        value = {'nsid': 1, 'name': params['namespace']['bdev_name']}
        subsys['namespaces'][0] = value

    def _mock_remove_ns(self, params):
        subsys = self._find_subsys(params['nqn'])
        if isinstance(subsys, bytes):
            return subsys

        subsys['namespaces'][0] = None

    def _mock_delete_subsystem(self, params):
        del self.subsystems[params['nqn']]

    def _mock_add_referral(self, params):
        self.referrals.append(params)

    def _mock_remove_referral(self, params):
        for i, rf in enumerate(self.referrals):
            if (rf['nqn'] == params['nqn'] and
                    rf['addr'] == params['addr'] and
                    rf['port'] == params['port']):
                del self.referrals[i]
                return

    def _mock_get_subsystems(self, params):
        ret = []
        for nqn, value in self.subsystems.items():
            elem = {'nqn': nqn, **value}
            hosts = elem.pop('hosts')
            elem['allow_any_host'] = hosts[0][0]
            elem['hosts'] = [host[0] for host in hosts[1:]]
            ret.append(elem)

        return json.dumps({'result': ret}).encode('utf8')

    @staticmethod
    def _find_host(lst, host):
        for i, elem in enumerate(lst):
            if elem[0] == host:
                return i

    def _mock_add_host(self, params):
        host = params['host']
        subsys = self._find_subsys(params['nqn'])

        if isinstance(subsys, bytes):
            return subsys
        elif self._find_host(subsys['hosts'], host) is not None:
            return b'{"error": "host already present"}'

        key = params.get('psk')
        if key is not None:
            with open(key, 'r') as file:
                key = file.read()

        subsys['hosts'].append((host, key))

    def _mock_remove_host(self, params):
        host = params['host']
        subsys = self._find_subsys(params['nqn'])

        if isinstance(subsys, bytes):
            return subsys

        hosts = subsys['hosts']
        idx = self._find_host(hosts, host)
        if idx is None:
            return b'{"error": "host not present in nqn"}'

        subsys['hosts'] = hosts[:idx] + hosts[idx + 1:]

    def _mock_allow_any_host(self, params):
        subsys = self._find_subsys(params['nqn'])
        if isinstance(subsys, bytes):
            return subsys

        subsys['hosts'][0] = (params['allow_any_host'], None)

    def loop(self, timeout=None):
        rd, _, _ = select.select([self.sock], [], [], timeout)
        if not rd:
            return False

        buf = self.sock.recv(2048)
        obj = json.loads(buf)

        method = obj['method']
        handler = self.handlers.get(method)
        if handler is None:
            self.sock.sendall(b'{"error":"method not found"}')
            return True

        try:
            rv = handler(obj.get('params', {}))
            if rv is None:
                rv = b'{"result":0}'
            self.sock.sendall(rv)
        except Exception:
            self.sock.sendall(b'{"error":"unexpected error"}')

        return True
