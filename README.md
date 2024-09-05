# About the charm

This charm implements an NVMe-oF gateway that integrates with Ceph storage to
provide a highly available (HA) target that exports Rados Block Devices (RBD) as
NVME disks.

# Usage

## Configuration

The file `config.yaml` specifies the list of options and their descriptions and
default values.

## Deployment

This assumes a pre-existing Ceph cluster.

Before we can use the charm proper, we'll need to relate the NVMe charm to the
charm that provides the `ceph-client` relation endpoint. As of this writing, there
are 2 known charms: `ceph-mon` and `microceph`. Therefore, we run the following
to have the charm operational:

    juju relate ceph-mon ceph-nvme

## Actions

The NVMe charm's actions can be divided into 3: Actions to manage endpoints,
actions to manage hosts and miscellaneous actions.

### create-endpoint

An endpoint can be created provided an RBD pool already exists in the Ceph
cluster that the charm is related to. In order to create it, we run the
following:

    juju run ceph-nvme/0 create-endpoint \
        rbd-image=my-image rbd-pool=my-pool units="'3'"

This will create the endpoint and use 3 units for the NVMe application that
will be used to provide high-availability.

When the `units` parameter is an integer, the application will select randomly
from the list of units. Otherwise, we can pass a comma-separated list to be
specific in terms of the units we want to use:

    juju run ceph-nvme/0 create-endpoint \
        rbd-image=my-image rbd-pool=my-pool units=ceph-nvme/1,ceph-nvme/2

The output of this action includes:
  - The NQN of the endpoint (A unique identifier)
  - The IP address and port of the endpoint
  - The effective number of units that are involved in handling the endpoint

### delete-endpoint

This action removes a previously created endpoint. To run this action, we specify
the NQN of the endpoint we want to delete. Note that this only deletes the endpoint
on the unit we run this on.

    juju run ceph-nvme/0 delete-endpoint nqn=my-nqn

### list-endpoint

This action lists all the endpoints that the unit it's called on is managing. It
reports the NQN, IP address and port of each endpoint.

### join-endpoint

This action is meant to let new units manage endpoints that were previously created
by other units. By passing the endpoint NQN, the running unit will start managing
this endpoint as well and contribute to its high availability.

### add-host

Once an endpoint is created, we can allow-list hosts so that they can connect to it.
To do so, we pass the host's own NQN, the endpoint's NQN and a password (or key).

    juju run ceph-nvme/0 add-host hostnqn=host-nqn nqn=endpoint-nqn key=dhchap-key

This will make it possible for the host to connect to the endpoint with the specified
NQN on any unit that manages it.

The host's NQN is stored in different places depending on the connector used. For the
nvme-cli utility, it's usually at `/etc/nvme/hostnqn`.

In addition, if the host NQN is set to 'any', every host will be allowed, and no further
checks will be performed.

### delete-host

This action removes a previously added host from an endpoint, both described
by their NQN's.

### list-hosts

This action lists the hosts that were added for an endpoint.

### reset-target

This action stops the SPDK target, clears all its associated configuration and
then restarts it. After the action completes, the unit on which this was run
will be managing no endpoints. It is meant to recover from a situation in which
the target isn't function properly and the operator considers that starting
anew is better.

### pause

This pauses all NVMe-oF services, preventing further changes and preventing
new hosts from connecting to the endpoints of the unit.

### resume

Resumes a previously paused unit.

## Connecting to an endpoint

Any tool that implements the NVMe-oF protocol on the initiator side can be
used to connect to an endpoint. The most common one is the `nvme-cli`
utility. In order to connect to an endpoint, we can run the following:

    sudo nvme connect -t tcp -n my-nqn -a ip-addr -s ip-port --dhchap-secret=key

Where the NQN, IP address and port can be retrieved from the output of the
`list-endpoints` or `create-endpoint` actions, and the DH-CHAP key is the one
used in the `add-host` action.

This will yield a device path like `/dev/nvme0n1`, which will be backed by
the RBD pool and image that was specified when the endpoint was created.

## Discovery service

Note that the above connecting process works for one address/port pair
at a time. If we have several units backing the same endpoint, we can
run the same command with a different address/pair port (assuming the
NQN is the same), and we'll add an additional path to the existing device
instead of creating a new device.

However, such a process can be cumbersome. So we can instead run the discovery
command, which will list all the available paths for an endpoint:

    sudo nvme discover -t tcp -a ip-addr -s ip-port

If the above command shows more than one endpoint, then we can run the
`connect-all` command to connect to all endpoints at once:

    sudo nvme connect-all -t tcp -a ip-addr -s ip-port -n my-nqn --dhchap-secret=key

And afterwards, the device will be backed by _all_ units.

## Transition from the ceph-iscsi charm

The ceph-iscsi charm is deprecated and no new features will be added. The ceph-nvme
charm was designed to be its successor. In order to transition from the old charm
to the new, a few things have to be kept in mind:

- The ceph-nvme charm uses NQN's to identify endpoints, whereas the ceph-iscsi charm
  uses IQN's. 
- For access control, the ceph-iscsi charm requires users to specify a username and
  password, and then has an action called `add-trusted-ip` that allows list any
  number of IP addresses. The ceph-nvme charm works by allowing host NQNs instead of
  addresses, and is able to remove hosts at any time.
- The ceph-nvme charm supports some features that the ceph-iscsi charm does not, like
  the ability to add new units to support an existing endpoint, or the ability to
  list and remove endpoints. While this is possible with the `gwcli` utility for the
  ceph-iscsi charm, the ceph-nvme charm presents these features via juju actions.
