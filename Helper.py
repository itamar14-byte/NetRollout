import os
import datetime
import socket
import ipaddress

NOW = datetime.datetime.now()
LOGFILE = NOW.strftime("rollout_%Y%m%d_%H%M%S.log")

def log(name,string):
    with open(name, "a") as file:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        file.write(f"{timestamp}   {string}\n")


def validate_file_extension(path, extension):
    if not os.path.isfile(path):
        print(f"\033[91m{path}\033[0m is not a file\033[0m")
        log(LOGFILE,path + " is not a file")
        return False
    if not path.lower().endswith(extension):
        print(f"\033[91mfile must be {extension}\033[0m")
        log(LOGFILE,path + "must be " + extension)
        return False
    return True


def validate_device_data(device):
    try :
        ipaddress.ip_address(device['ip'])
    except ValueError:
        print(f"\033[91m{device['ip']}\033[0m is not a valid IPv4 address")
        return False

    if not (device['port'].isnumeric() and 0 < int(device['port'])<= 65535) :
        print(f"\033[91m{device['port']}\033[0m is not a valid port number")
        return False

    if device['device_type'] not in {"fortinet", "paloalto_panos", "cisco_ios", "cisco_nxos", "cisco_xe", "cisco_xr",
                                     "juniper_junos", "arista_eos", "aruba_aoscx", "checkpoint_gaia", "hp_procurve",
                                     "hp_comware"}:
        print(f"\033[91m{device['device_type']}\033[0m is not supported")
        return False
    return True



def test_tcp_port(ip, port=22):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as conn:
        conn.settimeout(5)

        try:
            conn.connect((ip, port))
            conn.close()
            return True
        except Exception:
            return False