import os
import datetime
import socket

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


def test_tcp_port(ip, port=22):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as conn:
        conn.settimeout(5)

        try:
            conn.connect((ip, port))
            conn.close()
            return True
        except Exception:
            return False