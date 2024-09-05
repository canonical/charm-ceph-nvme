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
import os
import shutil
import socket
import subprocess
import tempfile

logger = logging.getLogger(__name__)


class RPC:
    """See https://spdk.io/doc/jsonrpc_proxy.html
       for the format used in RPC."""
    id_ = 1

    def __getattr__(self, name):
        def _inner(**kwargs):
            id_ = self.id_
            self.id_ = (id_ + 1) % 100
            base = {'id': id_, 'method': name}
            if kwargs:
                base['params'] = kwargs
            return base

        return _inner


def default_cpuset(cpus):
    """By default, use half of the available cores."""
    rlen = -(len(cpus) // -2)
    return cpus[:rlen]


def compute_cpuset(spec):
    cpuset = spec.strip()
    cpus = list(os.sched_getaffinity(0))

    if not cpuset:
        return default_cpuset(cpus)
    elif cpuset.startswith('['):
        # List of cpus on which to run the target.
        try:
            cpuset = json.loads(cpuset)
            cpuset = list(set(cpuset).intersect(cpus))
            if not cpuset:
                cpuset = default_cpuset(cpus)
            return cpuset
        except Exception:
            logger.warning('invalid CPU set specified. Using default')
            return default_cpuset(cpus)
    else:
        # Number of CPUs on which to run the target.
        try:
            nr_cpus = int(cpuset)
            return cpus[:nr_cpus]
        except ValueError:
            logger.warning('invalid CPU set specified. Using default')
            return default_cpuset(cpus)


def compute_cpumask(cpuset):
    """Compute a binary mask given a list of CPUs."""
    mask = 0
    for cpu in cpuset:
        mask |= 1 << cpu
    return mask


def _systemd_service_from_path(file_path):
    return os.path.basename(file_path)


def create_systemd_svc(file_path, contents, **kwargs):
    service = _systemd_service_from_path(file_path)
    with tempfile.NamedTemporaryFile(mode='w+') as file:
        file.write(contents.format(**kwargs))
        file.flush()
        shutil.copy(file.name, file_path)
        subprocess.check_call(['sudo', 'systemctl', 'daemon-reload'])
        subprocess.check_call(['sudo', 'systemctl', 'enable', service])
        subprocess.check_call(['sudo', 'systemctl', 'start', service])


def create_dir(path):
    """Create a directory and all the needed parents."""
    subprocess.check_call(['sudo', 'mkdir', '-p', path])


def get_adrfam(addr):
    try:
        socket.inet_pton(socket.AF_INET, addr)
        return socket.AF_INET, 'IPv4'
    except OSError:
        socket.inet_pton(socket.AF_INET6, addr)
        return socket.AF_INET6, 'IPv6'


def get_free_port(address='127.0.0.1'):
    """Get a free port and the family for an IP address."""
    family, fstr = get_adrfam(address)
    s = socket.socket(family, socket.SOCK_DGRAM)
    s.bind((address, 0))
    _, *extra = s.getsockname()
    s.close()
    return extra[0], fstr
