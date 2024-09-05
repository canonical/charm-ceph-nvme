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
import unittest

import zaza.model as zaza_model
import zaza.openstack.utilities.generic as zaza_utils
import zaza.openstack.charm_tests.test_utils as test_utils


def setup_osds_and_pools():
    for unit in zaza_model.get_units('microceph'):
        action_obj = zaza_model.run_action(
            unit_name=unit.entity_id,
            action_name='add-osd',
            action_params={'loop-spec': '1G,1'})
        zaza_utils.assertActionRanOK(action_obj)

    cmds = ['sudo microceph.ceph osd pool create mypool',
            'sudo microceph.rbd create --size 1024 mypool/myimage']
    for cmd in cmds:
        zaza_model.run_on_unit('microceph/0', cmd)

    states = {
        'ubuntu': {
            'workload-status': 'active',
            'workload-status-message-prefix': ''
        },
        'microceph': {
            'workload-status': 'active',
            'workload-status-message-prefix': ''
        }
    }
    zaza_model.wait_for_application_states(states=states)


class CephNVMETest(test_utils.BaseCharmTest):

    def _install_nvme(self, unit):
        zaza_model.run_on_unit(unit, 'sudo apt update')

        cmd = 'sudo apt install %s-$(uname -r)'
        zaza_model.run_on_unit(unit, cmd % 'linux-modules')
        zaza_model.run_on_unit(unit, cmd % 'linux-modules-extra')
        zaza_model.run_on_unit(unit, 'sudo modprobe nvme-core')
        zaza_model.run_on_unit(unit, 'sudo modprobe nvme-tcp')
        zaza_model.run_on_unit(unit, 'sudo apt install nvme-cli')
        out = zaza_model.run_on_unit(unit, 'ls /dev/nvme-fabrics')
        return out.get('Code', 1)

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

        # Finally, re-join the endpoint we just deleted.
        action_obj = zaza_model.run_action(
            unit_name='ceph-nvme/1',
            action_name='join-endpoint',
            action_params={'nqn': data['nqn']})
        zaza_utils.assertActionRanOK(action_obj)
        d2 = action_obj.data['results']
        self.assertEqual(d2['nqn'], data['nqn'])

        # Mount the device on the Ubuntu unit.
        if self._install_nvme('ubuntu/0') != 0:
            # Unit doesn't have the nvme-fabrics driver - Abort.
            raise unittest.SkipTest('Skipping test due to lack of NVME driver')

        cmd = 'sudo nvme discover -t tcp -a %s -s %s -o json'
        out = zaza_model.run_on_unit('ubuntu/0', cmd %
                                     (data['address'], data['port']))
        out = json.loads(out.get('Stdout'))
        records = out['records']
        self.assertEqual(records[0]['nqn'], data['nqn'])

        cmd = 'sudo nvme connect-all -t tcp -a %s -s %s -o json'
        out = zaza_model.run_on_unit('ubuntu/0', cmd %
                                     (data['address'], data['port']))
        out = json.loads(out.get('Stdout'))
        for elem in out['Subsystems']:
            if elem['nqn'] == data['nqn']:
                addr1 = 'traddr=%s trsvcid=%s' % (data['address'],
                                                  data['port'])
                addr2 = 'traddr=%s trsvcid=%s' % (d2['address'], d2['port'])

                paths = elem['Paths']
                self.assertEqual(len(paths), 2)
                self.assertEqual(paths[0]['Transport'], 'tcp')
                self.assertEqual(paths[1]['Transport'], 'tcp')
                self.assertEqual(paths[0]['State'], 'live')
                self.assertEqual(paths[1]['State'], 'live')
                self.assertTrue(paths[0]['Address'] == addr1 or
                                paths[0]['Address'] == addr2)
                self.assertTrue(paths[1]['Address'] == addr2 or
                                paths[1]['Address'] == addr2)
                break
        else:
            raise RuntimeError('NQN %s not found' % data['nqn'])

        cmd = 'sudo nvme list -o json'
        out = zaza_model.run_on_unit('ubuntu/0', cmd)
        out = json.loads(out.get('Stdout'))
        for elem in out['Devices']:
            if 'SPDK' in elem['ModelNumber']:
                device = elem['DevicePath']
                break
        else:
            raise RuntimeError('Device not found')

        msg = 'Hello there!'
        zaza_model.run_on_unit('ubuntu/0',
                               'echo "%s" | sudo tee %s' % (msg, device))

        cmd = 'sudo dd if=%s of=/dev/stdout count=%d status=none' % (
            device, len(msg))
        out = zaza_model.run_on_unit('ubuntu/0', cmd)
        self.assertEqual(out.get('Stdout'), msg)
