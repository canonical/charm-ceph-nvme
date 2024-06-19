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

import os
import subprocess
import sys

HUGEPAGES = '/proc/sys/vm/nr_hugepages'
HUGEPAGE_TARGET = 2048


def setup_hugepages():
    try:
        with open(HUGEPAGES, 'r') as file:
            num = int(file.read())
            if num >= HUGEPAGE_TARGET:
                return True
        rv, _ = subprocess.getstatusoutput('echo %d | sudo tee > %s' %
                                           (HUGEPAGE_TARGET, HUGEPAGES))
        return rv == 0
    except Exception as exc:
        print("failed to setup huge pages: %s" % str(exc))
        return False


xname = sys.argv[1]
args = sys.argv[2:]
if not setup_hugepages():
    print("warning: running without huge pages. expect a performance decrease")
    args.extend(['--no-huge', '-s', str(4096)])

# Replace the current process with a call to the target.
os.execv(xname, [os.path.basename(xname)] + args)
