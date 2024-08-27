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
import logging
import select
import socket


class MockSPDK:
    def __init__(self, sock):
        new_sock, _ = sock.accept()
        sock.close()
        self.sock = new_sock
        self.logger = logging.getLogger('spdk')
        self.rbds = {}
        self.bdevs = {}
        self.clusters = set()
        self.nvmf_subsys = {}

    @staticmethod
    def _list_rmidx(lst, ix):
        return lst[:ix] + lst[ix + 1:]

    @staticmethod
    def dict_eq(x, y, keys):
        for k in keys:
            if k not in x or k not in y or x[k] != y[k]:
                return False
        return True

    def nvmf_create_transport(self, **kwargs):
        pass

    def nvmf_create_subsystem(self, **kwargs):
        nqn = kwargs['nqn']
        if nqn in self.nvmf_subsys:
            raise ValueError('NQN already present')

        self.nvmf_subsys[nqn] = {'listen_addresses': [], 'namespaces': [],
                                 'allow_any_host': False,
                                 'referrals': [], 'hosts': [], **kwargs}

    def nvmf_delete_subsystem(self, **kwargs):
        del self.nvmf_subsys[kwargs['nqn']]

    def nvmf_subsystem_add_listener(self, **kwargs):
        addrs = self.nvmf_subsys[kwargs['nqn']]['listen_addresses']
        addrs.append(kwargs['listen_address'])

    def nvmf_subsystem_remove_listener(self, **kwargs):
        subsys = self.nvmf_subsys[kwargs['nqn']]
        ls = subsys['listener_addresses']
        for ix, listener in enumerate(ls):
            if self.dict_eq(listener, kwargs,
                            ('traddr', 'trtype', 'adrfam')):
                break
        else:
            raise ValueError('listener not found')

        subsys['listen_addresses'] = self._list_rmidx(ls, ix)

    def nvmf_subsystem_add_ns(self, **kwargs):
        nqn = kwargs.pop('nqn')
        ns = kwargs['namespace']
        ns['name'] = ns['bdev_name']
        self.nvmf_subsys[nqn]['namespaces'].append(ns)

    def nvmf_get_subsystems(self, **kwargs):
        return list(self.nvmf_subsys.values())

    def nvmf_subsystem_remove_ns(self, **kwargs):
        nqn, nsid = kwargs['nqn'], kwargs['nsid']
        subsys = self.nvmf_subsys[nqn]
        listeners = subsys['listen_addresses']
        if nsid > len(listeners):
            raise ValueError('NSID does not exist')

        subsys['listen_addresses'] = self._list_rmidx(listeners, nsid - 1)

    def nvmf_discovery_add_referral(self, **kwargs):
        nqn = kwargs['subnqn']
        self.nvmf_subsys[nqn]['referrals'].append(kwargs['address'])

    def nvmf_discovery_remove_referral(self, **kwargs):
        nqn = kwargs['subnqn']
        subsys = self.nvmf_subsys[nqn]
        for ix, ref in enumerate(subsys['referrals']):
            if self.dict_eq(ref, kwargs['address'],
                            ('traddr', 'trsvcid', 'trtype')):
                break
        else:
            raise ValueError('referral not found')

        subsys['referrals'] = self._list_rmidx(subsys['referrals'], ix)

    def nvmf_subsystem_allow_any_host(self, **kwargs):
        self.nvmf_subsys[kwargs['nqn']]['allow_any_host'] = (
            kwargs['allow_any_host'])

    def nvmf_subsystem_add_host(self, **kwargs):
        self.nvmf_subsys[kwargs['nqn']]['hosts'].append(kwargs['host'])

    def nvmf_subsystem_remove_host(self, **kwargs):
        subsys = self.nvmf_subsys[kwargs['nqn']]
        for ix, host in enumerate(subsys['hosts']):
            if host == kwargs['host']:
                break
        else:
            raise ValueError('host not found')

        subsys['hosts'] = self._list_rmidx(subsys['hosts'], ix)

    def bdev_rbd_create(self, **kwargs):
        name = kwargs['name']
        if name in self.rbds:
            raise ValueError('RBD bdev already present')

        cluster = kwargs['cluster_name']
        if cluster not in self.clusters:
            raise ValueError('cluster does not exist')
        self.rbds[name] = kwargs

    def bdev_rbd_delete(self, **kwargs):
        del self.rbds[kwargs['name']]

    def bdev_rbd_register_cluster(self, **kwargs):
        name = kwargs['name']
        if name in self.clusters:
            raise ValueError('cluster already registered')
        self.clusters.add(name)

    def loop(self, timeout=None):
        rd, _, _ = select.select([self.sock], [], [], timeout)
        if not rd:
            return False

        buf = self.sock.recv(2048)
        obj = json.loads(buf)

        name = obj['method']
        method = getattr(self, name, None)
        if method is None:
            err = {'error': 'method %s not found' % name}
            self.sock.sendall(json.dumps(err).encode('utf8'))
            return True

        try:
            ret = {'result': method(**obj.get('params', {}))}
        except Exception as exc:
            ret = {'error': str(exc)}

        self.sock.sendall(json.dumps(ret).encode('utf8'))
        return True
