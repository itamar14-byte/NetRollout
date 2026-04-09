import ipaddress
import os
import socket
import time

from logging_utils import RolloutLogger


class Validator:
    def __init__(self, logger: RolloutLogger):
        self.logger = logger

    # Defines supported platforms for app
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

    def validate_file_extension(self,path: str, extension: str) -> bool:
        """
        This function validates the file extensions part of file parsing,
         and makes sure the files are correct and fit the expected type
        :param path: file path provided by the user
        :param extension: expected file type - csv for _devices, txt for _commands
        :return: True if file extension is correct, False otherwise
        """
        if not os.path.isfile(path):
            # Verifies file extension indeed exists in the system
            # and is a recognized file type
            # (not directory or something else)
            self.logger.notify(f"{path} is not a file", "red")
            return False
        if not path.lower().endswith(extension):
            # Verifies the type of the file indeed conforms to the extension we expect for the file
            self.logger.notify(f"file must be {extension}", "red")
            return False
        return True


    @staticmethod
    def validate_ip(ip: str) -> bool:
        """checks that address is a valid ip"""
        try:
            ipaddress.ip_address(ip)
            return True
        except ValueError:
            return False


    @staticmethod
    def validate_port(port: str) -> bool:
        """checks that port is a valid port number in the tcp IETF range"""
        if not port.isnumeric():
            return False
        elif int(port) < 1 or int(port) > 65535:
            return False
        return True


    @staticmethod
    def validate_platform(platform: str) -> bool:
        """checks that platform is supported by the app"""
        if platform not in Validator.SUPPORTED_PLATFORMS:
            return False
        return True


    def validate_device_data(self,device: dict[str, str]) -> bool:
        """
        This function runs as part of the device files parsing and is used to validate values of the device data,
        when unpacking the csv iterable of dictionaries into a list. As we run on the provided _devices, the function checks
        applicable values such as ip address and tcp port and makes sure they are in correct format
         In that case, notifications will be added to SSE _queue
        :param device: device dictionary unpacked from csv file
        :return: True if device data is correct, False otherwise
        """
        # Uses the ipaddress library to _verify the ip address is in the X.X.X.X ipv4
        # format,
        # such that x is an int in the range 0-255
        if Validator.validate_ip(device["ip"]):
            # Verifies supplied port number matches expected TCP port values - a number in the 1-65535 range

            if Validator.validate_port(device["port"]):
                # Checks that the supplied device type matches the list of supported platforms

                if Validator.validate_platform(device["device_type"]):
                    # If all required validations pass, the function returns true and the device may be parsed
                    return True

                else:
                    self.logger.notify(
                        f"{device['device_type']} is not supported",
                        "red")

            else:
                self.logger.notify(f"{device['port']} is not a valid port "
                                   f"number", "red")

        else:
            self.logger.notify(f"{device['ip']} is not a valid IPv4 address",
                               "red")

        return False


    @staticmethod
    def test_tcp_port(ip: str, port: int = 22) -> bool:
        """
        A wrapper for the socket libray used to test connectivity to the device over the supplied SSH port
        :param ip: ip address of the device
        :param port: TCP port used for SSH connection
        :return: True if the device is reachable, and false otherwise
        """
        # Connection will run for 3 attempts, with a 1-second delay between tries.
        # A fresh socket is created per attempt — reusing a failed socket raises WinError 10056 on Windows.
        for attempt in range(Validator.TCP_RETRIES):
            # Creates a Connection object which will be used to probe the device. Connection is gracefully closed by socket
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as conn:
                    conn.settimeout(Validator.TCP_TIMEOUT)
                    # Tries a connection to the device over the supplied ip and port
                    conn.connect((ip, port))
                    # Returns True if the connection is successful.
                    # Otherwise, socket throws an exception and a device is deemed
                    # unreachable
                    return True
            except OSError:
                if attempt < Validator.TCP_RETRIES - 1:
                    time.sleep(Validator.TCP_RETRY_DELAY)
                    continue
        return False

