import ansible
import argparse
import collections
import csv
import os
import re
import sys
import yaml

from ansible.plugins.callback import CallbackBase
from ansible.parsing.dataloader import DataLoader
from ansible.module_utils.common.collections import ImmutableDict
from ansible.inventory.manager import InventoryManager
from ansible.executor.playbook_executor import PlaybookExecutor
from ansible.vars.manager import VariableManager

# custom callbacks
import ansible_callbacks

# you can change these
ANSIBLE_INVENTORY = ".inventory.yml"
ANSIBLE_PLAYBOOK = "playbook.yml"


class Host:
    def __init__(self, **kwargs):
        for kwarg in kwargs.items():
            setattr(self, *kwarg)

    def has_fields(self, fields):
        for k, v in fields.items():
            if not re.match(v.lower(), getattr(self, k.lower()).lower()):
                return False
        return True


class Task(Host):
    pass


class ResultsCollector(CallbackBase):
    def __init__(self, hosts, *args, **kwargs):
        super(ResultsCollector, self).__init__(*args, **kwargs)
        self.hosts = hosts

    def get_host(self, result):
        return self.hosts[str(result._host)]

    def get_task(self, result):
        return self.hosts[str(result._host)].tasks[str(result._task.name)]

    def v2_runner_on_ok(self, result, *args, **kwargs):
        task = self.get_task(result)

        # run the callback HERE
        stdout_callback = getattr(ansible_callbacks, task.name)

        # save results
        task.callback_result = stdout_callback(str(result._result.get("stdout")))
        task.exit_status = str(result._result.get("rc"))
        task.task_failed = False

    def v2_runner_on_failed(self, result, *args, **kwargs):
        task = self.get_task(result)

        # log errors
        task.exit_status = str(result._result.get("rc"))
        task.task_failed = True

        print(f"Error: {task.name} failed", file=sys.stderr)

    def v2_runner_on_unreachable(self, result, ignore_errors=False):
        host = self.get_host(result)
        task = self.get_task(result)

        # log errors
        task.exit_status = str(result._result.get("rc"))
        task.task_failed = True

        print(f"Error: {host.name} ({host.public_ip_address}) unreachable", file=sys.stderr)


class Azure:
    def __init__(self):
        self.hosts = {}

    def load_from_csv(self, filename):
        try:
            document = open(filename, mode="r")

            # first line
            delimiter = next(document).strip().split("=")[1]

            # second line
            fieldlist = (
                next(document)
                .strip(delimiter + " \n")
                .lower()
                .replace(" ", "_")
                .split(delimiter)
            )

            rows = csv.DictReader(document, fieldnames=fieldlist, delimiter=delimiter)

            # dict of objects
            self.hosts = {row["name"]: Host(**row, tasks={}) for row in rows}

        except IOError:
            print("Open Azure document failed", file=sys.stderr)
            sys.exit(1)

    def load_playbook(self, filename):
        try:
            document = open(filename, mode="r")
            playbook = yaml.safe_load(document)

            for host in self.hosts.values():
                for task in playbook[0]["tasks"]:
                    host.tasks[task["name"].lower()] = Task(
                        **task,
                        callback_result=None,
                        exit_status=None,
                        task_failed=None,
                    )

        except IOError:
            print("Open playbook failed", file=sys.stderr)
            sys.exit(1)

    def filter_hosts(self, fields):
        if fields == None:
            # no fields returns the whole list of hosts
            return self.hosts

        hosts = {}

        for host in self.hosts.values():
            if host.has_fields(fields):
                hosts[host.name] = host
        return hosts

    def generate_yaml(self, filename, fields=None):
        try:
            inventory = open(filename, "w+")

            hosts = self.filter_hosts(fields)

            # ansible dict for all hosts
            ansible_dict = {}

            for host in hosts.values():
                ansible_dict[host.name] = {
                    "ansible_host": host.public_ip_address,
                    "ansible_port": 22,
                    "ansible_user": "ubuntu",
                    "ansible_ssh_private_key_file": f"/var/lib/jenkins/.ts/admin/rsa/azure/{host.name}/{host.name}",
                }

            # dump the inventory in yaml
            yaml.dump({"all": {"hosts": ansible_dict}}, inventory)

            # get the inventory of selected hosts
            return hosts

        except IOError:
            print("Open inventory failed", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":

    # initialize argument parser
    parser = argparse.ArgumentParser(
        description="Generate a configuration report from VMs running in Azure.\n\n"
        "The report will only be generated for Linux VMs.\n\n",
        epilog="Author: Edwy Mandret <edwy.mandret@traydstream.com>",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("azure_hosts", help="Azure VMs in CSV format")
    parser.add_argument("report_file", help="Report in CSV format")

    # parse arguments
    args = parser.parse_args()

    # TEST
    inventory = Azure()
    inventory.load_from_csv(args.azure_hosts)
    inventory.load_playbook(ANSIBLE_PLAYBOOK)

    hosts = inventory.generate_yaml(
        ANSIBLE_INVENTORY,
        {
            "resource_group": "rg-production",
            "status": "running",
            "operating_system": "linux",
            "public_ip_address": r"\d+\.\d+\.\d+\.\d+",
        },
    )

    # since the API is constructed for CLI it expects certain options to always be set in the context object
    ansible.context.CLIARGS = ImmutableDict(
        become=None,
        become_method=None,
        become_user=None,
        check=False,
        connection="smart",
        diff=False,
        forks=10,
        module_path=None,
        start_at_task=None,
        syntax=None,
        verbosity=0,
    )

    loader = DataLoader()
    inventory = InventoryManager(loader=loader, sources=[ANSIBLE_INVENTORY])
    variable_manager = VariableManager(loader=loader, inventory=inventory)

    executor = PlaybookExecutor(
        inventory=inventory,
        loader=loader,
        passwords={},
        playbooks=[ANSIBLE_PLAYBOOK],
        variable_manager=variable_manager,
    )

    executor._tqm._stdout_callback = ResultsCollector(hosts)
    executor.run()

    # host has been populated NOW

    # exit if hosts is empty
    if hosts == {}:
        sys.exit(0)

    with open(args.report_file, mode="w+") as report:

        # get callback names and desired fields
        callbacks = hosts[next(iter(hosts))].tasks.keys()
        fieldlist = ["name", "resource_group", "public_ip_address"]

        writer = csv.writer(report, delimiter=",", quoting=csv.QUOTE_MINIMAL)

        # write header
        writer.writerow(map(lambda k: k.upper().replace("_", " "), [*fieldlist, *callbacks]))

        # values to write in the csv file
        values = []

        for host in hosts.values():
            values.extend([getattr(host, field) for field in fieldlist])
            values.extend([host.tasks[cb].callback_result for cb in callbacks])
            writer.writerow(values)
