# Copyright 2024 Luciano
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

from mock import patch, Mock
import unittest
import unittest.mock as mock

import ops
import ops.testing

import src.charm as charm


class TestCharm(unittest.TestCase):
    def setUp(self):
        self.harness = ops.testing.Harness(charm.CephNVMECharm)
        self.addCleanup(self.harness.cleanup)

    @mock.patch.object(charm.subprocess, 'check_call')
    @mock.patch.object(charm.utils, 'create_systemd_svc')
    def test_start(self, check_call, create_systemd_svc):
        # Simulate the charm starting
        self.harness.begin_with_initial_hooks()
        # Ensure we set an ActiveStatus with no message
        self.assertEqual(self.harness.model.unit.status,
                         ops.ActiveStatus('ready'))
