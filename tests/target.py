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

import zaza.model as zaza_model
import zaza.openstack.utilities.generic as zaza_utils
import zaza.openstack.charm_tests.test_utils as test_utils


def setup_osds_and_pools():
    for unit in zaza_model.get_units('microceph'):
        action_obj = zaza_model.run_action(
            unit_name=unit.entity_id,
            action_name='add-osd',
            action_params={'loop-spec': '4G,1'})
        zaza_utils.assertActionRanOK(action_obj)

    cmds = ['sudo microceph.ceph osd pool create mypool',
            'sudo microceph.rbd create --size 4096 mypool/myimage']
    for cmd in cmds:
        zaza_model.run_on_unit('microceph/0', cmd)


class CephNVMETest(test_utils.BaseCharmTest):

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

        return   # XXX: Write the rest of the test

        # Finally, re-join the endpoint we just deleted.
        action_obj = zaza_model.run_action(
            unit_name='ceph-nvme/1',
            action_name='join-endpoint',
            action_params={'nqn': data['nqn']})
        zaza_utils.assertActionRanOK(action_obj)

        # Mount the device on the Ubuntu unit.
        zaza_model.run_on_unit('ubuntu/0', 'sudo snap install nvme-cli')
        out = zaza_model.run_on_unit('ubuntu/0',
                                     'sudo nvme-cli discover -t tcp '
                                     '-a %s -s %s' % (data['address'],
                                                      data['port']))
        out = json.loads(out.get('Stdout'))
        records = out['records']
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]['nqn'], data['nqn'])
