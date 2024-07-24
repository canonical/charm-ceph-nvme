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
import os
import socket
import tempfile
import threading
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

        self.file = tempfile.NamedTemporaryFile(mode='w+')
        self.proxy_thread = threading.Thread(target=self._thread_entry)
        self.proxy_thread.start()

        new_sock, _ = rpc_sock.accept()
        rpc_sock.close()
        self.spdk = utils.MockSPDK(new_sock)

        self.spdk.loop()
        self.local_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.local_sock.bind(('127.0.0.1', 0))

    def tearDown(self):
        self.local_sock.sendto(b'{"method":"stop"}', PROXY_ADDR)
        self.proxy_thread.join()
        self.local_sock.close()
        self.spdk.close()

    def _thread_entry(self):
        srv = proxy.Proxy(LOCAL_PORT, self.file.name, '127.0.0.1', LOCAL_SOCK)
        srv.serve()

    def msgloop(self, msg):
        msg = json.dumps(msg).encode('utf8')
        self.local_sock.sendto(msg, PROXY_ADDR)

        while self.spdk.loop(0.3):
            pass

        return json.loads(self.local_sock.recv(2048))

    def test_rpcs(self):
        msg = self.rpc.cluster_add(
            name='ceph', user='client', key='ABC123', mon_host='1.1.1.1')
        rv = self.msgloop(msg)
        self.assertNotIn("error", rv)

        msg = self.rpc.create(
            cluster='ceph', pool_name='mypool', rbd_name='myimage')
        rv = self.msgloop(msg)
        self.assertNotIn('error', rv)
        prev = rv

        msg = self.rpc.find(nqn=prev['nqn'])
        rv = self.msgloop(msg)
        self.assertEqual(rv.get('pool'), 'mypool')
        self.assertEqual(rv.get('image'), 'myimage')
        self.assertEqual(rv.get('cluster'), 'ceph')
        self.assertEqual(rv.get('addr'), '127.0.0.1')
        self.assertIn('port', rv)

        msg = self.rpc.join(
            nqn='nqn.1', addresses=[{'addr': '127.0.0.1', 'port': 65001}])
        rv = self.msgloop(msg)
        self.assertNotIn('error', rv)

        msg = self.rpc.host_add(host='host', nqn=prev['nqn'], key='some-key')
        rv = self.msgloop(msg)
        self.assertNotIn('error', rv)

        subsys = self.spdk.subsystems[prev['nqn']]
        for host, key in subsys['hosts']:
            if key == 'some-key':
                self.assertEqual(host, 'host')
                break
        else:
            raise KeyError('host not found')

        msg = self.rpc.host_add(host='*', nqn=prev['nqn'])
        rv = self.msgloop(msg)
        self.assertNotIn('error', rv)
        self.assertTrue(self.spdk.subsystems[prev['nqn']]['hosts'][0])

        msg = self.rpc.host_del(host='host', nqn=prev['nqn'])
        rv = self.msgloop(msg)
        self.assertNotIn('error', rv)

        msg = self.rpc.leave(nqn='nqn.1', addr='127.0.01', port=65001)
        self.msgloop(msg)
        self.assertFalse(self.spdk.referrals)

        rv = self.msgloop(self.rpc.list())
        self.assertEqual(len(rv), 1)
        self.assertEqual(rv[0]['type'], 'rbd')

        msg = self.rpc.remove(nqn=prev['nqn'])
        rv = self.msgloop(msg)
        self.assertNotIn('error', rv)
