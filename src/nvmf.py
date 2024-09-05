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
import subprocess
import sys

sys.path.append(os.path.dirname(os.path.abspath(__name__)))
import utils

HUGEPAGES = '/proc/sys/vm/nr_hugepages'


def setup_hugepages(target):
    try:
        with open(HUGEPAGES, 'r') as file:
            num = int(file.read())
            if num >= target:
                return True
        rv, _ = subprocess.getstatusoutput('echo %d | sudo tee > %s' %
                                           (target, HUGEPAGES))
        return rv == 0
    except Exception as exc:
        print("failed to setup huge pages: %s" % str(exc))
        return False


def main():
    xname = sys.argv[1]
    config_path = sys.argv[2]
    args = [os.path.basename(xname)]

    with open(config_path, 'r') as file:
        config = json.loads(file.read())

    nr_hugepages = config.get('nr-hugepages', 0)
    if not nr_hugepages:
        args.extend(['--no-huge', '-s', str(4096)])
    elif not setup_hugepages(config.get('nr-hugepages', 0)):
        print("warning: running without huge pages. expect a performance hit")
        # Aim for at least 4G for the target.
        args.extend(['--no-huge', '-s', str(4096)])

    cpuset = utils.compute_cpuset(config.get('cpuset'))
    args.extend(['-m', str(hex(utils.compute_cpumask(cpuset)))])

    # Replace the current process with a call to the target.
    os.execv(xname, args)


if __name__ == '__main__':
    main()
