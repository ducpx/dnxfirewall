#!/usr/bin/env python3

from __future__ import annotations

import os
import sys
import time
import json
import socket

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional
from subprocess import run as srun, DEVNULL, CalledProcessError

from dnx_gentools.def_constants import HOME_DIR, INITIALIZE_MODULE, hardout, str_join
from dnx_gentools.file_operations import ConfigurationManager, load_data, write_configuration, json_to_yaml

from dnx_routines.configure.iptables import IPTablesManager
from dnx_routines.logging.log_client import Log

# ===============
# TYPING IMPORTS
# ===============
if (TYPE_CHECKING):
    from dnx_gentools.file_operations import ConfigChain

def lprint(sep: str = '-'): print(f'{sep}' * 32)


@dataclass
class Args:
    v: int = 0
    verbose: int = 0
    u: int = 0
    update: int = 0
    packages: int = 0

    @property
    def verbose_set(self):
        return self.v or self.verbose

    @property
    def update_set(self):
        return self.u or self.update


LOG_NAME: str = 'system'
PROGRESS_TOTAL_COUNT: int = 4

LINEBREAK: str = '-' * 32

SYSTEM_DIR:  str = f'{HOME_DIR}/dnx_system'
UTILITY_DIR: str = f'{HOME_DIR}/dnx_system/utils'

# ----------------------------
# UTILS
# ----------------------------
def sprint(s: str, /) -> None:
    '''setup print. includes timestamp before arg str.
    '''
    print(f'{time.strftime("%H:%M:%S")}| {s}')

def eprint(s: str, /) -> None:
    '''error print. includes timestamp and alert before arg str.
    '''
    print(f'{time.strftime("%H:%M:%S")}| !!! {s}')
    while True:
        answer: str = input('continue? [y/N]: ')
        if (answer.lower() == 'y'):
            return

        elif (answer.lower() in ['n', '']):
            hardout()

        else:
            print('!invalid selection.')

def dnx_run(s: str, /) -> None:
    '''convenience function, subprocess run wrapper adding additional args.
    '''
    try:
        if (args.verbose_set):
            srun(s, shell=True, check=True)

        else:
            srun(s, shell=True, stdout=DEVNULL, stderr=DEVNULL, check=True)

    except CalledProcessError as cpe:
        eprint(f'{cpe}')

def check_run_as_root() -> None:
    if (os.getuid()):

        eprint('must run dnxfirewall auto loader as root. exiting...')

def check_dnx_user() -> None:
    with open('/etc/passwd', 'r') as passwd_f:
        passwd: list[str] = passwd_f.read().splitlines()

    if not any([usr for usr in passwd if usr.split(':', 1)[0] == 'dnx']):

        eprint('dnx user does not exist. create user and clone repo into dnx home directory before running.')

def check_clone_location() -> None:
    if (not os.path.isdir(HOME_DIR)):

        eprint('dnxfirewall filesystem must be located at /home/dnx.')

def check_already_ran() -> None:
    with ConfigurationManager('system') as dnx:
        dnx_settings: ConfigChain = dnx.load_configuration()

    if (not args.update_set and dnx_settings['auto_loader']):

        eprint('dnxfirewall has already been installed. exiting...')

    elif (args.update_set):
        eprint('dnxfirewall has not been installed. see readme for guidance. exiting...')

# ----------------------------
# PROGRESS BAR
# ----------------------------
# starting at -1 to compensate for the first process
bar_len: int = 30
completed_count: int = -1
def progress(desc: str) -> None:
    global completed_count

    completed_count += 1
    ratio: float = completed_count / PROGRESS_TOTAL_COUNT

    filled_len: int = int(bar_len * ratio)
    bar: str = '#' * filled_len + '=' * (bar_len - filled_len)

    sys.stdout.write(f'{completed_count}/{PROGRESS_TOTAL_COUNT} |')
    sys.stdout.write(f'| [{bar}] {int(100 * ratio)}% |')
    sys.stdout.write(f'| {desc.ljust(36)}\r')
    sys.stdout.flush()

# ============================
# INTERFACE CONFIGURATION
# ============================
# convenience function wrapper for physical interface to dnxfirewall zone association.
def configure_interfaces() -> None:
    interfaces_detected: list[str] = check_system_interfaces()

    user_intf_config: dict[str, str] = collect_interface_associations(interfaces_detected)
    public_dns_servers: dict = load_data('dns_server.cfg')['resolvers']

    set_dnx_interfaces(user_intf_config)
    set_dhcp_interfaces(user_intf_config)

    with open(f'{SYSTEM_DIR}/interfaces/intf_config_template.cfg', 'r') as intf_configs_f:
        intf_configs: str = intf_configs_f.read()

    for intf_name, intf in user_intf_config.items():
        intf_configs = intf_configs.replace(f'_{intf_name}_', intf)

    # storing the modified template containing specified interface names.
    # this will be used to configure wan interface via webui or change system level dns servers.
    write_configuration(json.loads(intf_configs), 'interfaces', filepath='dnx_system/interfaces')

    # setting public dns servers on the interface so the system itself will use the user configured
    # servers in the web ui.
    dns1: str = public_dns_servers['primary']['ip_address']
    dns2: str = public_dns_servers['secondary']['ip_address']

    yaml_output: str = json_to_yaml(intf_configs, is_string=True)
    yaml_output = yaml_output.replace('_PRIMARY__SECONDARY_', f'{dns1},{dns2}')

    write_net_config(yaml_output)

def check_system_interfaces() -> list[str]:
    interfaces_detected = [intf[1] for intf in socket.if_nameindex() if 'lo' not in intf[1]]

    if (len(interfaces_detected) < 3):
        eprint(f'at least 3 interfaces are required to deploy dnxfirewall. detected: {len(interfaces_detected)}.')

    return interfaces_detected

def collect_interface_associations(interfaces_detected: list[str]) -> dict[str, str]:
    print(f'{LINEBREAK}\navailable interfaces\n{LINEBREAK}')

    for i, interface in enumerate(interfaces_detected, 1):
        print(f'{i}. {interface}')

    print(LINEBREAK)

    # build out full json for interface configs as dict
    interface_config: dict[str, str] = {'WAN': '', 'LAN': '', 'DMZ': ''}
    while True:
        for int_name in interface_config:
            while True:
                select = input(f'select {int_name} interface: ')
                if (select.isdigit() and int(select) in range(1, len(interfaces_detected)+1)):
                    interface_config[int_name] = interfaces_detected[int(select)-1]
                    break

        if confirm_interfaces(interface_config):
            if len(set(interface_config.values())) == 3:
                break

            eprint('interface definitions must be unique.')

    return interface_config

# takes interface config as dict, converts to yaml, then writes to system folder
def write_net_config(interface_configs: str) -> None:
    sprint('configuring netplan service...')

    # write config file to netplan
    with open('/etc/netplan/01-dnx-interfaces.yaml', 'w') as intf_config:
        intf_config.write(interface_configs)

    # removing the default configuration set during os install.
    try:
        os.remove('/etc/netplan/00-installer-config.yaml')
    except:
        pass

# modifying dnx configuration files with the user specified interface names and their corresponding zones
def set_dnx_interfaces(user_intf_config: dict[str, str]) -> None:
    sprint('setting dnx interface configurations...')

    with ConfigurationManager('system') as dnx:
        dnx_settings: ConfigChain = dnx.load_configuration()

        for zone, intf in user_intf_config.items():
            dnx_settings[f'interfaces->builtins->{zone.lower()}->ident'] = intf

        dnx.write_configuration(dnx_settings.expanded_user_data)

def set_dhcp_interfaces(user_intf_config: dict[str, str]) -> None:
    with ConfigurationManager('dhcp_server') as dhcp:
        dhcp_settings: ConfigChain = dhcp.load_configuration()

        for zone in ['LAN', 'DMZ']:

            dhcp_settings[f'interfaces->builtins->{zone.lower()}->ident'] = user_intf_config[zone]

        dhcp.write_configuration(dhcp_settings.expanded_user_data)

def confirm_interfaces(interface_config: dict[str, str]) -> bool:
    print(' '.join([f'{zone}={intf}' for zone, intf in interface_config.items()]))
    while True:
        answer = input('confirm? [Y/n]: ')
        if (answer.lower() in ['y', '']):
            return True

        else:
            return False

# ============================
# INSTALL PACKAGES
# ============================
def install_packages() -> list:

    commands = [
        ('sudo apt install python3-pip -y', 'setting up python3'),
        ('pip3 install flask uwsgi', 'installing python web app framework'),
        ('sudo apt install nginx -y', 'installing web server driver'),
        ('sudo apt install libnetfilter-queue-dev libnetfilter-conntrack-dev libmnl-dev net-tools -y',
            'installing networking components'),
        ('pip3 install Cython', 'installing C extension language (Cython)')
    ]

    return commands

def compile_extensions() -> list:

    commands: list[tuple[str, str]] = [
        (f'sudo python3 {HOME_DIR}/dnx_run.py compile dnx-nfqueue', 'compiling dnx-nfqueue'),
        (f'sudo python3 {HOME_DIR}/dnx_run.py compile cfirewall', 'compiling cfirewall'),
        (f'sudo python3 {HOME_DIR}/dnx_run.py compile hash-trie', 'compiling dnx-hash_trie'),
        (f'sudo python3 {HOME_DIR}/dnx_run.py compile cprotocol-tools', 'compiling cprotocol tools'),
    ]

    return commands

def configure_webui() -> list:
    cert_subject: str = str_join([
        '/C=US',
        '/ST=Arizona',
        '/L=cyberspace',
        '/O=dnxfirewall',
        '/OU=security',
        '/CN=dnx.rules',
        '/emailAddress=help@dnxfirewall.com'
    ])

    generate_cert_commands: str = ' '.join([
        f'sudo openssl req -x509 -nodes -days 365 -newkey rsa:2048',
        f'-keyout {SYSTEM_DIR}/ssl/dnx-web.key',
        f'-out {SYSTEM_DIR}/ssl/dnx-web.crt',
        f'-subj {cert_subject}'
    ])

    commands: list[tuple[str, Optional[str]]] = [
        (generate_cert_commands, 'generating dnx webui ssl certificate'),
        (f'sudo cp {UTILITY_DIR}/dnx_web /etc/nginx/sites-available/', 'configuring management webui'),
        ('ln -s /etc/nginx/sites-available/dnx_web /etc/nginx/sites-enabled/', None),
        ('sudo rm /etc/nginx/sites-enabled/default', None)
    ]

    return commands

# ============================
# PERMISSION CONFIGURATION
# ============================

def set_permissions() -> None:

    progress('configuring dnxfirewall permissions')

    commands: list[str] = [

        # creating database file here, so it can get its permissions modified.
        # this will also ensure it won't be overridden by update pulls.
        f'touch {SYSTEM_DIR}/data/dnxfirewall.sqlite3',

        # set the dnx filesystem owner to the dnx user/group
        f'chown -R dnx:dnx {HOME_DIR}',

        # apply file permissions 750 on folders, 640 on files
        f'chmod -R 750 {HOME_DIR}',
        f'find {HOME_DIR} -type f -print0|xargs -0 chmod 640',

        # setting the dnx command line utility as executable
        f'chmod 750 {HOME_DIR}/dnx_run.py',

        # creating simlink to allow dnx command from anywhere if logged in as dnx user
        f'ln -s {HOME_DIR}/dnx_run.py /usr/local/bin/dnx',

        # adding www-data user to dnx group
        'usermod -aG dnx www-data',

        # reverse of above
        'usermod -aG www-data dnx'
    ]

    for command in commands:
        dnx_run(command)

    # update sudoers to allow dnx user no pass for specific system functions
    no_pass = [
        'dnx ALL = (root) NOPASSWD: /usr/sbin/iptables-restore',
        'dnx ALL = (root) NOPASSWD: /usr/sbin/iptables-save',
        'dnx ALL = (root) NOPASSWD: /usr/sbin/iptables',
        # 'dnx ALL = (root) NOPASSWD: /usr/bin/systemctl status *'
    ]

    for line in no_pass:
        dnx_run(f'echo "{line}" | sudo EDITOR="tee -a" visudo')

# ============================
# SERVICE FILE SETUP
# ============================
def set_services() -> None:
    ignore_list = ['dnx-database-psql.service', 'dnx-syslog.service']

    progress('creating dnxfirewall services')

    services = os.listdir(f'{UTILITY_DIR}/services')
    for service in services:

        if (service not in ignore_list):

            dnx_run(f'cp {UTILITY_DIR}/services/{service} /etc/systemd/system/')
            dnx_run(f'systemctl enable {service}')

    dnx_run(f'systemctl enable nginx')

# ============================
# INITIAL IPTABLES SETUP
# ============================
def configure_iptables() -> None:
    progress('loading default iptables')

    with IPTablesManager() as iptables:
        iptables.apply_defaults(suppress=True)

# ============================
# CLEANUP
# ============================
def mark_completion_flag() -> None:
    with ConfigurationManager('system') as dnx:
        dnx_settings: ConfigChain = dnx.load_configuration()

        dnx_settings['auto_loader'] = True

        dnx.write_configuration(dnx_settings.expanded_user_data)

# TODO: add code to pull mac from wan interface and set it in the config file stored in the usr dir.
def store_default_mac():
    pass

def run():
    global PROGRESS_TOTAL_COUNT

    if (not args.update_set):
        configure_interfaces()

    # will hold all dynamically set commands prior to execution to get an accurate count for progress bar.
    dynamic_commands: list[tuple[str, Optional[str]]] = []

    if (not args.update_set):
        dynamic_commands.extend(configure_webui())

    # packages will be installed during initial installation automatically.
    # if update is set, the default is to not update packages.
    if (not args.update_set) or (args.update_set and args.packages):
        dynamic_commands.extend(install_packages())

    dynamic_commands.extend(compile_extensions())

    PROGRESS_TOTAL_COUNT += len([1 for k, v in dynamic_commands if v])

    action = 'update' if args.update_set else 'deployment'
    sprint(f'starting dnxfirewall {action}...')
    lprint()

    for command, desc in dynamic_commands:

        if (desc):
            progress(desc)

        dnx_run(command)

    # iptables and permissions will be done for install and update
    configure_iptables()
    set_permissions()

    if (not args.update_set):
        set_services()
        mark_completion_flag()

    progress('dnxfirewall installation complete')
    sprint('\ncontrol of the WAN interface configuration has been taken by dnxfirewall.')
    sprint('use the webui to configure a static ip or enable ssh access if needed.')
    sprint('restart the system then navigate to https://192.168.83.1 from LAN to manage.')

    hardout()


if INITIALIZE_MODULE('autoloader'):
    try:
        args = Args(**{a: 1 for a in os.environ['PASSTHROUGH_ARGS'].split(',') if a})
    except Exception as E:
        hardout(f'DNXFIREWALL arg parse failure => {E}')

    # pre-checks to make sure application can run properly
    check_run_as_root()
    check_dnx_user()
    check_clone_location()

    # initializing log module which is required when using ConfigurationManager
    Log.run(name=LOG_NAME)
    ConfigurationManager.set_log_reference(Log)

    # this uses the config manager, so must be called after log initialization
    check_already_ran()