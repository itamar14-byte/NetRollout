import os
import datetime
import socket
import ipaddress
import time

# Creates a timestamp for the defined globally at every running of Core.py.
# variable will be calculated when importing helper
LOGFILE = datetime.datetime.now().strftime("rollout_%Y%m%d_%H%M%S.log")

# Defines supported platform for app
SUPPORTED_PLATFORMS = {
    "fortinet",
    "paloalto_panos",
    "cisco_ios",
    "cisco_nxos",
    "cisco_xe",
    "cisco_xr",
    "juniper_junos",
    "arista_eos",
    "aruba_aoscx",
    "checkpoint_gaia",
    "hp_procurve",
    "hp_comware",
}


TCP_TIMEOUT = 5
TCP_RETRIES = 3
TCP_RETRY_DELAY = 1

RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
END = "\033[0m"
colors = {
    "RED": RED,
    "GREEN": GREEN,
    "YELLOW": YELLOW,
}


def msg(string: str, color: str) -> str:
    """Adds ANSI escape sequences to terminal color for progress and error messages"""
    color = colors.get(color.upper())
    if color:
        return color + string + END
    return string


def log(string: str, file_name: str = LOGFILE) -> None:
    """
    A logging function that writes a message to a logfile with
     the globally configured name and attaches the message to a timestamp
    :param file_name: TIme stamped name for the logfile that is written too when log is called
    :param string: message to write in the log
    """
    with open(file_name, "a") as file:
        # Sets the current timestamp for the time of call and adds the stamped message to the log file
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        file.write(f"{timestamp}\t{string}\n")


def validate_file_extension(path: str, extension: str) -> bool:
    """
    This function validates the file extensions part of file parsing,
     and makes sure the files are correct and fit the expected type
    :param path: file path provided by the user
    :param extension: expected file type - csv for devices, txt for commands
    :return: True if file extension is correct, False otherwise
    """
    if not os.path.isfile(path):
        # Verifies file extension indeed exists in the system
        # and is a recognised file type (not directory or something else)
        print(msg(f"{path} is not a file", "red"))
        log(path + " is not a file")
        return False
    if not path.lower().endswith(extension):
        # Verifies the type of the file indeed conforms to the extension we expect for the file
        print(msg(f"file must be {extension}", "red"))
        log(path + "must be " + extension)
        return False
    return True


def validate_ip(ip: str) -> bool:
    """checks that address is a valid ip"""
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def validate_port(port: str) -> bool:
    """checks that port is a valid port number in the tcp IETF range"""
    if not port.isnumeric():
        return False
    elif int(port) < 0 or int(port) > 65535:
        return False
    return True


def validate_platform(platform: str) -> bool:
    """checks that platform is supported by the app"""
    if platform.lower() not in SUPPORTED_PLATFORMS:
        return False
    return True


def validate_device_data(device: dict[str, str]) -> bool:
    """
    This function runs as part of the device files parsing and is used to validate values of the device data,
    when unpacking the csv iterable of dictionaries into a list. As we run on the provided devices, the function checks
    applicable values such as ip address and tcp port and makes sure they are in correct format
    :param device: device dictionary unpacked from csv file
    :return: True if device data is correct, False otherwise
    """
    # Uses ipaddress library to verify the ip address is in the X.X.X.X ipv4 format,
    # such that x is an int in the range 0-255
    if validate_ip(device["ip"]):
        # Verifies supplied port number matches expected TCP port values - a number in the 1-65535 range

        if validate_port(device["port"]):
            # Checks that the supplied device type matches the list of supported platforms

            if validate_platform(device["device_type"]):
                # If all required validations pass, the function returns true and the device may be parsed
                return True

            else:
                print(msg(f"{device['device_type']} is not supported", "red"))
                log(device["device_type"] + " is not supported")

        else:
            print(msg(f"{device['port']} is not a valid port number", "red"))
            log(device["device_type"] + " is not a valid port number")

    else:
        print(msg(f"{device['ip']} is not a valid IPv4 address", "red"))
        log(device["ip"] + " is not a valid IPv4 address")

    return False


def test_tcp_port(ip: str, port: int = 22) -> bool:
    """
    A wrapper for the socket libray used to test connectivity to the device over the supplied SSH port
    :param ip: ip address of the device
    :param port: TCP port used for SSH connection
    :return: True if device is reachable, False otherwise
    """
    # Creates a Connection object which will be used to probe the device. Connection is gracefully closed by socket
    # Connection will run for 3 attempts, with a 1-second delay between tries
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as conn:
        conn.settimeout(TCP_TIMEOUT)
        for attempt in range(TCP_RETRIES):
            # Tries a connection to the device over the supplied ip and port
            try:
                conn.connect((ip, port))
                # Returns True if the connection is successful.
                # Otherwise, socket thrown an exception and device is deemed unreachable
                return True
            except OSError:
                if attempt < TCP_RETRIES - 1:
                    time.sleep(TCP_RETRY_DELAY)
                    continue
        return False
