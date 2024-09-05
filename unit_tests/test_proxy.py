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
import multiprocessing
import os
import socket
import tempfile
import time
import unittest

import src.proxy as proxy
from . import utils


LOCAL_SOCK = '/tmp/proxy-test.sock'
LOCAL_PORT = 65000
PROXY_ADDR = ('127.0.0.1', LOCAL_PORT)


class TestProxy(unittest.TestCase):
    def setUp(self):
        self.rpc = proxy.utils.RPC()
        if os.path.exists(LOCAL_SOCK):
            os.unlink(LOCAL_SOCK)

        rpc_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        rpc_sock.bind(LOCAL_SOCK)
        rpc_sock.listen(1)

        def _mp_spdk(sock):
            spdk = utils.MockSPDK(sock)
            while True:
                try:
                    spdk.loop()
                except Exception:
                    break

        def _mp_proxy(out):
            wdir = tempfile.TemporaryDirectory()
            wname = wdir.name
            config_path = os.path.join(wname, 'config.json')

            with open(config_path, 'w') as file:
                file.write('{"proxy-port":%d}' % LOCAL_PORT)

            p = proxy.Proxy(config_path, LOCAL_SOCK)
            out.append(1)
            p.serve()

        mgr = multiprocessing.Manager()
        out = mgr.list()

        self.spdk = multiprocessing.Process(target=_mp_spdk, args=(rpc_sock,))
        self.spdk.start()
        self.proxy = multiprocessing.Process(target=_mp_proxy, args=(out,))
        self.proxy.start()
        self.local_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.local_sock.bind(('127.0.0.1', 0))

        while len(out) < 1:
            time.sleep(0)

    def tearDown(self):
        self.local_sock.sendto(b'{"method":"stop"}', PROXY_ADDR)
        self.proxy.join()
        self.local_sock.close()
        self.spdk.kill()
        self.spdk.join()

    def msgloop(self, msg):
        msg = json.dumps(msg).encode('utf8')
        self.local_sock.sendto(msg, PROXY_ADDR)
        return json.loads(self.local_sock.recv(2048))

    def test_rpcs(self):
        msg = self.rpc.cluster_add(
            name='ceph', user='client', key='ABC123', mon_host='1.1.1.1')
        rv = self.msgloop(msg)
        self.assertNotIn("error", rv)

        msg = self.rpc.create(
            nqn='nqn.1', cluster='ceph', pool_name='mypool',
            rbd_name='myimage', addr='0.0.0.0')
        rv = self.msgloop(msg)
        self.assertNotIn('error', rv)
        prev = rv

        msg = self.rpc.find(nqn=prev['nqn'])
        rv = self.msgloop(msg)
        self.assertEqual(rv.get('pool'), 'mypool')
        self.assertEqual(rv.get('image'), 'myimage')
        self.assertEqual(rv.get('cluster'), 'ceph')
        self.assertEqual(rv.get('addr'), '0.0.0.0')
        self.assertIn('port', rv)

        msg = self.rpc.join(
            nqn='nqn.1', addresses=[{'addr': '127.0.0.1', 'port': 65001}],
            addr='127.0.0.1')
        rv = self.msgloop(msg)
        self.assertNotIn('error', rv)

        msg = self.rpc.host_add(host='host', nqn=prev['nqn'],
                                dhchap_key='some-key')
        rv = self.msgloop(msg)
        self.assertNotIn('error', rv)

        msg = self.rpc.host_list(nqn=prev['nqn'])
        rv = self.msgloop(msg)
        self.assertEqual(rv, ['host'])

        msg = self.rpc.host_add(host='any', nqn=prev['nqn'])
        rv = self.msgloop(msg)
        self.assertNotIn('error', rv)

        msg = self.rpc.host_list(nqn=prev['nqn'])
        rv = self.msgloop(msg)
        self.assertEqual('any', rv)

        msg = self.rpc.host_del(host='host', nqn=prev['nqn'])
        rv = self.msgloop(msg)
        self.assertNotIn('error', rv)

        msg = self.rpc.leave(subsystems=[
            dict(nqn='nqn.1', addr='127.0.0.1', port=65001)])
        rv = self.msgloop(msg)
        self.assertNotIn('error', rv)

        rv = self.msgloop(self.rpc.list())
        self.assertEqual(len(rv), 1)
        self.assertEqual(rv[0]['type'], 'rbd')

        msg = self.rpc.remove(nqn=prev['nqn'])
        rv = self.msgloop(msg)
        self.assertNotIn('error', rv)
