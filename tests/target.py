# Copyright 2024 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Encapsulate Ceph-NVMe-oF testing."""

import json
import tenacity
import unittest

import zaza.model as zaza_model
import zaza.openstack.utilities.generic as zaza_utils
import zaza.openstack.charm_tests.test_utils as test_utils


def setup_osds_and_pools():
    cmds = ['sudo ceph osd crush rule rm replicated_rule',
            'sudo ceph osd crush rule create-replicated replicated_rule '
            'default osd',
            'sudo ceph osd erasure-code-profile rm default',
            'sudo ceph osd erasure-code-profile set default '
            'plugin=jerasure k=2 m=1 crush-failure-domain=osd']
    for cmd in cmds:
        zaza_model.run_on_unit('ceph-mon/0', cmd)

    loops = []
    for file in ('l1', 'l2', 'l3'):
        zaza_model.run_on_unit('ceph-osd/0', 'touch %s' % file)
        zaza_model.run_on_unit('ceph-osd/0', 'truncate --size 2G ./%s' % file)
        out = zaza_model.run_on_unit('ceph-osd/0',
                                     'sudo losetup -fP --show ./%s' % file)
        loops.append(out['Stdout'].strip())

    for loop in loops:
        zaza_model.run_action_on_leader('ceph-osd', 'add-disk',
                                        action_params={'osd-devices': loop})
    zaza_model.wait_for_application_states()

    cmds = ['sudo ceph osd pool create mypool',
            'sudo rbd create --size 1024 mypool/myimage']
    for cmd in cmds:
        zaza_model.run_on_unit('ceph-mon/0', cmd)

    zaza_model.wait_for_application_states()


class CephNVMETest(test_utils.BaseCharmTest):
    HOST_NQN = (
        'nqn.2014-08.org.nvmexpress:uuid:c1d418fc-4177-4034-880e-f24fb539a14b')
    HOST_KEY = (
        'DHHC-1:00:XW5dAgFfSfwRbMsUmA/1ApLw61q+XJVHlnTYitGXmbzt7CGB:')

    def _install_nvme(self, unit):
        zaza_model.run_on_unit(unit, 'sudo apt update')
        release = zaza_model.run_on_unit(unit, 'uname --kernel-release')
        release = release['Stdout'].strip()

        cmd = 'sudo apt install -y %s-' + release
        zaza_model.run_on_unit(unit, cmd % 'linux-modules')
        zaza_model.run_on_unit(unit, cmd % 'linux-modules-extra')

        zaza_model.run_on_unit(unit, 'sudo modprobe nvme-core')
        zaza_model.run_on_unit(unit, 'sudo modprobe nvme-tcp')
        zaza_model.run_on_unit(unit, 'sudo modprobe nvme-fabrics')
        zaza_model.run_on_unit(unit, 'sudo apt install -y nvme-cli')
        out = zaza_model.run_on_unit(unit, 'ls /dev/nvme-fabrics')
        return int(out.get('Code', 1))

    @tenacity.retry(wait=tenacity.wait_fixed(1), reraise=True,
                    stop=tenacity.stop_after_attempt(10),
                    retry=tenacity.retry_if_exception_type(ValueError))
    def _nvme_connect(self, unit, data):
        # As usual with the nvme-cli, some commands can fail for no
        # apparent reason, so we retry this one a couple of times.
        cmd = 'sudo nvme connect -t tcp -a %s -s %s -n %s -q %s -S %s'
        out = zaza_model.run_on_unit(unit, cmd %
                                     (data['address'], data['port'],
                                      data['nqn'], self.HOST_NQN,
                                      self.HOST_KEY))
        if int(out.get('Code', 1)) != 0:
            raise ValueError('failed to connect')

    def test_mount_device(self):
        # Create an endpoint with both units.
        action_obj = zaza_model.run_action(
            unit_name='ceph-nvme/0',
            action_name='create-endpoint',
            action_params={'rbd-pool': 'mypool', 'rbd-image': 'myimage',
                           'units': '2'})
        zaza_utils.assertActionRanOK(action_obj)
        data = action_obj.data['results']

        # Remove the endpoint on the second unit.
        action_obj = zaza_model.run_action(
            unit_name='ceph-nvme/1',
            action_name='delete-endpoint',
            action_params={'nqn': data['nqn']})
        zaza_utils.assertActionRanOK(action_obj)

        # Test that the second unit backs no endpoints.
        action_obj = zaza_model.run_action(
            unit_name='ceph-nvme/1',
            action_name='list-endpoints')
        zaza_utils.assertActionRanOK(action_obj)
        self.assertEqual(action_obj.data['results']['endpoints'], '[]')

        # Finally, re-join the endpoint we just deleted.
        action_obj = zaza_model.run_action(
            unit_name='ceph-nvme/1',
            action_name='join-endpoint',
            action_params={'nqn': data['nqn']})
        zaza_utils.assertActionRanOK(action_obj)
        d2 = action_obj.data['results']
        self.assertEqual(d2['nqn'], data['nqn'])

        # Allow a host with a key.
        action_obj = zaza_model.run_action(
            unit_name='ceph-nvme/0',
            action_name='add-host',
            action_params={'nqn': data['nqn'], 'hostnqn': self.HOST_NQN,
                           'dhchap-key': self.HOST_KEY})
        zaza_utils.assertActionRanOK(action_obj)

        # Mount the device on one unit.
        if self._install_nvme('ceph-osd/0') != 0:
            # Unit doesn't have the nvme-fabrics driver - Abort.
            raise unittest.SkipTest('Skipping test due to lack of NVME driver')

        self._nvme_connect('ceph-osd/0', data)
        self._nvme_connect('ceph-osd/0', d2)

        out = zaza_model.run_on_unit('ceph-osd/0',
                                     'sudo nvme list-subsys -o json')
        out = json.loads(out.get('Stdout'))[0]['Subsystems'][0]['Paths']
        self.assertEqual(len(out), 2)

        device = zaza_model.run_on_unit('ceph-osd/0',
                                        'sudo nvme list -o json')
        device = json.loads(device.get('Stdout'))['Devices'][0]['DevicePath']
        msg = 'Hello there!'
        zaza_model.run_on_unit('ceph-osd/0',
                               'echo "%s" | sudo tee %s' % (msg, device))

        cmd = 'sudo dd if=%s of=/dev/stdout count=%d status=none' % (
            device, len(msg))
        out = zaza_model.run_on_unit('ceph-osd/0', cmd)
        self.assertTrue(out.get('Stdout').startswith(msg))
