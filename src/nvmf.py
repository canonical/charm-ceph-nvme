#!/usr/bin/env python3

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
