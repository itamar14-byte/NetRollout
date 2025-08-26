import csv
import netmiko
from napalm import get_network_driver
import argparse
from Helper import log,test_tcp_port,validate_file_extension, LOGFILE


def parse_files(device_path, commands_path,verbose=False):
    device_path = device_path.strip('"')
    commands_path = commands_path.strip('"')
    if validate_file_extension(commands_path,"txt") and validate_file_extension(device_path,"csv"):
        try:
            with open(device_path,"r",encoding="utf-8-sig") as file:
                required_keys = {"ip", "username", "password", "device_type","secret", "port"}
                reader = csv.DictReader(file)
                missing_keys = required_keys - set(reader.fieldnames)
                if missing_keys:
                    print(list(reader))
                    raise ValueError("Missing keys: {}".format(missing_keys))
                devices=[item for item in reader]

            with open(commands_path,"r") as file:
                 commands = file.readlines()

            if verbose:
                print(f"\033[92mDevices file successfully processed. {len(devices)} devices found\033[0m")
                print(f"\033[92mDevices file successfully processed. {len(commands)} commands will be executed\033[0m")
            log(LOGFILE, "Devices file successfully processed." + str(len(devices)) + "devices found")
            log(LOGFILE, "Devices file successfully processed." + str(len(commands)) + "commands will be executed")

            return devices, commands

        except FileNotFoundError:
            print(f"\033[91mfile not found\033[0m")
            return [], []

        except PermissionError:
            print(f"\033[91mcan't access file\033[0m")
            return [], []

        except Exception as e:
            print(f"\033[91mParsing failed: {e}\033[0m")
    else:
        return [], []

def push_config(devices, commands, verbose=False):

    print(test_tcp_port("172.16.1.157", 22))
    for device in devices:
        if verbose:
            print(f"connecting to {device['ip']}:{device['port']}")
        log(LOGFILE, "connecting to" + device['ip'] + ":" + device['port'])
        if test_tcp_port(device['ip'], int(device['port'])):
            try:
                net_connect = netmiko.ConnectHandler(**device)
                if verbose:
                    print(f"\033[92m{device['ip']} connected successfully\033[0m")
                log(LOGFILE, device['ip'] + " connected successfully")

                net_connect.enable()
                net_connect.config_mode()

                for command in commands:
                    output = net_connect.send_config_set([command.strip()], exit_config_mode=False)
                    errors = ["Invalid","unrecognized","unknown"]
                    if any(err.lower() in output or err.capitalize() in output for err in errors):
                        print(f"\033[91m{command} failed on {device['ip']}: {output}\033[0m")
                        log(LOGFILE, command + " failed on " + device['ip'])
                        continue

                net_connect.exit_config_mode()
                net_connect.save_config()
                net_connect.disconnect()


            except netmiko.NetMikoAuthenticationException:
                print(f"\033[91m{device['ip']} authentication failed\033[0m")
                log(LOGFILE, device['ip'] + " authentication failed")
                continue
            except netmiko.NetmikoTimeoutException:
                print(f"\033[91m{device['ip']} timed out\033[0m")
                log(LOGFILE, device['ip'] + " timed out")
                continue
            except Exception as e:
                print(f"\033[91m{device['ip']} failed: {e}\033[0m")
                log(LOGFILE, device['ip'] + " failed")
                continue

        else:
            print(f"\033[91m{device['ip']} is not reachable\033[0m")
            print(test_tcp_port(device["ip"], device["port"]))
            log(LOGFILE, device['ip'] + " is not reachable")

def fetch_config(device):
    netmiko_to_napalm = {"fortinet": "fortios", "paloalto_panos": "panos", "cisco_ios": "ios", "cisco_nxos": "nxos",
                         "cisco_xe": "iosxe", "cisco_xr": "iosxr", "juniper_junos": "junos", "arista_eos": "eos",
                         "aruba_aoscx": "aoscx", "checkpoint_gaia": False, "hp_procurve": "procurve",
                         "hp_comware": False}

    try:
        driver = get_network_driver(netmiko_to_napalm.get(device["device_type"]))
        node = driver(hostname=device["ip"], username=device["username"], password=device["password"],
                      optional_args={"secret": device["secret"]})
        node.open()
        config = node.get_config()["running"]
        node.close()
        return config

    except Exception as e:
        print(f"\033[91mcould not connect to {device['ip']}: {e}\033[0m")
        log(LOGFILE, "could not connect to " + device['ip'] + ": " + str(e))
        return False



def verify(devices, commands,verbose=False):

        successful_count = 0
        for device in devices:
            config = fetch_config(device)
            if config:
                rejects=[]
                for command in commands:
                    command = command.strip()
                    if command not in config:
                        rejects.append(command)
                        print(f"\033[91m{command} not configured on {device['ip']}\033[0m")
                        log(LOGFILE, command + " not configured on " + device['ip'])
                if not rejects:
                    if verbose:
                        print(f"\033[92m{device['ip']} successfully configured\033[0m")
                    log(LOGFILE, device['ip'] + "successfully configured")
                    successful_count += 1
        return successful_count



def get_args():
    parser = argparse.ArgumentParser(description="A Network Automation tool to roll out configuration snippets on a"
                                                 "set of devices.")
    parser.add_argument("-d", "--devices", help="Path to a csv file. Required fields are ip, platform,"
                                                " username, password, secret and SSH port")
    parser.add_argument("-c", "--commands", help="Path to a txt file. Must contain the requested commands,"
                                                 " in order")
    parser.add_argument("-vy", "--verify", help="Test configuration file to verify successful running",
                        action="store_const", const=True,
                        default=None)
    parser.add_argument("-vb", "--verbose", help="Prints logs to console", action="store_true")

    return parser.parse_args()


def main():


    args = get_args()
    devices_path = args.devices or input("Enter the device file path: ")
    commands_path = args.commands or input("Enter the commands file path: ")
    if args.verify is True:
        verify_rollout = args.verify
    else:
        verify_rollout = True if input("Do you want to verify roll out? (y/n): ").lower() == "y" else False
    verbose = args.verbose

    try:
        devices, commands = parse_files(devices_path, commands_path,verbose)
        if devices and commands:
            push_config(devices, commands,verbose)
            if verify_rollout:
                count = verify(devices, commands,verbose)
                if verbose:
                    print(f"\033[92m{count} devices successfully configured\033[0m")
                    print(f"\033[91m{len(devices)-count} devices with problems in configuration\033[0m")
                log(LOGFILE, str(count) + "devices successfully configured")
                log(LOGFILE, str(len(devices) - count) + "devices with problems in configuration")

        else:
            print(f"\033[91mDevice file invalid\033[0m")

    except ValueError as e:
        print(f"\033[91mDevice file invalid: {e}\033[0m")
main()

#add more validations - ip, device type, check if not supported by napalm