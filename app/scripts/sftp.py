def short_help():
    return "Interact with WPEngine SFTP server."


def run(args):
    import os
    import argparse
    commands = {
        'get-db': {'help': 'Imports and processes the database file', 'c': get_db},
        'sed-db': {'help': 'Reformats the database based on the wpe-config.json file.', 'c': sed_db},
        'get-plugins': {'help': 'Imports plugins that are not submodules', 'c': lambda args, env: get_folder('wp-content/plugins', env)},
        'get-themes': {'help': 'Imports themes that are not submodules', 'c': lambda args, env: get_folder('wp-content/themes', env)},
        'get': {'help': 'Get a remote file from the SFTP server', 'c': get},
        'put': {'help': 'Put a local file on the SFTP server', 'c': put}
    }

    def command(name):
        if name not in commands:
            raise argparse.ArgumentTypeError("Invalid command provided. See 'wpe-tool sftp' for more info.")
        return commands[name]['c']

    def environment(name):
        if name not in ['prod', 'staging']:
            raise argparse.ArgumentTypeError("Invalid environment specified.")
        return name

    usage = """wpe-tool sftp <command> [<additional_args>] [--env]

Commands:
"""
    for name, c in commands.items():
        usage += "   {:<15}  {}\n".format(name, c['help'])

    parser = argparse.ArgumentParser(
        description="Interacts with WPEngine SFTP servers.",
        usage=usage
    )
    parser.add_argument("command", type=command, help="Command to run")
    parser.add_argument("additional_args", type=str, nargs='*', help="Additional arguments for subcommands.")
    parser.add_argument("--env", type=environment, help="WPEngine environment to interact with (default: 'prod')", default='prod')

    h = []
    if len(args) > 1 and '--help' in args:
        args = [a for a in args if not a == '--help']
        h = ['--help']
    parsed_args = parser.parse_args(args)

    os.chdir('/app/volume/')
    parsed_args.command(args[1:] + h, parsed_args.env)


def _connect(env, wpe_config=None, wpe_secrets=None):
    """Creates a connection to the SFTP server.

    :param env: Pull from 'staging' or 'prod' servers.
    :param wpe_config: The wpe_config object. By default, loads from file.
    :param wpe_secrets: The wpe_secrets object. By default, loads from file.
    :returns: The PySFTP Connection object.
    """
    from lib import configure
    import pysftp, warnings
    warnings.filterwarnings("ignore", category=UserWarning, module='pysftp')
    from lib import configure
    if not wpe_config:
        wpe_config = configure.load_config()
    if not wpe_secrets:
        wpe_secrets = configure.load_secrets()

    if not env in wpe_secrets._sub:
        raise ValueError("Environment '{}' has not been configured to pull from SFTP. Run 'wpe-tool reconfigure secrets' to ammend this issue.")
    env_secrets = wpe_secrets._sub[env]

    cnopts = pysftp.CnOpts()
    cnopts.hostkeys = None
    connection_args = {
        'host': wpe_config['sftp_url'],
        'port': int(wpe_config['sftp_port']),
        'username': env_secrets['sftp_username'],
        'password': env_secrets['sftp_password'],
        'cnopts': cnopts
    }
    return pysftp.Connection(**connection_args)


def get_db(args, env):
    """Pulls the database from SFTP and runs the associated `sed` commands
    on it.

    :param args: Should be empty.
    :param env: The environment to use.
    """
    import os
    from lib import configure
    print("Importing database...")

    if not os.path.exists('/app/volume/.db'):
        os.makedirs('/app/volume/.db/')

    omysql = '.db/mysql.sql.original'
    nmysql = '.db/mysql.sql'

    print("Pulling mysql.sql from SFTP...")

    with _connect(env) as sftp:
        sftp.get(remotepath='wp-content/mysql.sql', localpath=omysql)

    sed_db([omysql, nmysql], env)


def sed_db(args, env):
    """Runs `sed` on the database. Note that we do not use internal Python
    regex functions because many of those cannot be applied in a
    streaming fashion as `sed` can.

    :param args: Array in the format [source, dest]
    :param env: Ignored
    """
    import argparse
    parser = argparse.ArgumentParser(
        description="Reformats the database based on the wpe-config.json file.",
    )
    parser.add_argument('source', type=str, help="Original mysql file. (default: '.db/mysql.sql.original')", default='.db/mysql.sql.original')
    parser.add_argument('dest', type=str, help="Reformatted mysql file. (default: '.db/mysql.sql')", default='.db/mysql.msql')
    parsed_args = parser.parse_args(args)

    from lib import configure
    import subprocess
    from urllib import parse

    omysql, nmysql = [parsed_args.source, parsed_args.dest]

    wpe_config = configure.load_config()

    print("Reformatting database for local use...")
    subprocess.run(["cp", omysql, nmysql])

    remote_url = parse.urlparse(wpe_config._sub[env]['url'])
    remote_netloc = remote_url.netloc.replace('.', r'\.')

    # I use sed to do replacement rather than some of the other tools
    # available since the MySQL files can get very large and Python
    # doens't seem to have anything for re.sub on large files.
    commands = []
    if wpe_config["wp_multisite_config"] == 'single-site' or wpe_config["wp_multisite_config"] == 'multisite-subfolder':
        local_url = parse.urlparse(wpe_config['virtual_hosts'])
        local_netloc = local_url.path.replace('.', r'\.')
        commands = [
            ["sed", "-i",
             r's/{}:\/\/{}/http:\/\/{}/g'.format(remote_url.scheme,
                                                 remote_netloc,
                                                 local_netloc),
             nmysql],
            ["sed", "-i",
             's/{}/{}/g'.format(remote_netloc, local_netloc),
             nmysql]
        ]
    elif wpe_config["wp_multisite_config"] == 'multisite-subdomain':
        local_urls = [parse.urlparse(vh) for vh in wpe_config['virtual_hosts'].split(',')]
        local_netloc = local_urls[0].path.replace('.', r'\.')
        commands = [
            ["sed", "-i",
             r"s/{}:\/\/\([a-zA-Z0-9]\+\.\)\?{}/http:\/\/\1{}/g".format(
                 remote_url.scheme, remote_netloc, local_netloc),
             nmysql],
            ["sed", "-i",
             r"s/\([a-zA-Z0-9]\+\.\)\?{}/\1{}/g".format(
                 remote_netloc, local_netloc),
             nmysql]
        ]

    for command in commands:
        subprocess.run(command)

    print("Done.")

def sftp_get_r(sftp, remote_path, local_path):
    """Helper command to recursively pull files from an SFTP server.
    Slower than PySFTP, but the syntax isn't confusing.

    """
    import os
    for filename in sftp.listdir(remote_path):
        remote_fpath = os.path.join(remote_path, filename)
        local_fpath = os.path.join(local_path, filename)
        if sftp.isdir(remote_fpath):
            os.makedirs(local_fpath)
            sftp_get_r(sftp, remote_fpath, local_fpath)
        else:
            sftp.get(remote_fpath, local_fpath)


def get_folder(path, env):
    """Helper command to recursively update a folder based on the WPEngine
    remote, while ignoring any folders that are Git repositories."""
    import os, shutil
    print("Selectively downloading folders from remote path '{}'.".format(path))
    with _connect(env) as sftp:
        for f in sftp.listdir(path):
            fpath = os.path.join(path, f)
            if sftp.isdir(fpath):
                if os.path.exists(fpath):
                    if os.path.exists(os.path.join(fpath, '.git')):
                        continue
                    else:
                        shutil.rmtree(fpath)
                print(" Downloading '{}'...".format(fpath))
                os.makedirs(fpath)
                sftp_get_r(sftp, fpath, fpath)
            else:
                if os.path.exists(fpath):
                    os.remove(fpath)
                print(" Downloading '{}'...".format(fpath))
                sftp.get(fpath, fpath)


def get(args, env):
    """Pulls content from the SFTP server."""
    import argparse
    parser = argparse.ArgumentParser(
        description="Gets files/directories from WPEngine SFTP server.",
    )
    parser.add_argument('remote_path', type=str, help="File to pull")
    parser.add_argument('local_path', type=str, help="Location to save file at.")
    parsed_args = parser.parse_args(args)

    remote_path = parsed_args.remote_path
    local_path = parsed_args.local_path if parsed_args.local_path else remote_path
    with _connect(env) as sftp:
        if sftp.isdir(remote_path):
            sftp_get_r(sftp, remote_path, local_path)
        else:
            sftp.get(sftp, remote_path, local_path)


def put(args, env):
    """Puts content onto the SFTP server."""
    parser = argparse.ArgumentParser(
        description="Saves files/directories to the WPEngine SFTP server.",
        usage=usage
    )
    parser.add_argument('local_path', type=str, help="Location to save file at.")
    parser.add_argument('remote_path', type=str, help="File to pull", required=False, default=None)
    parsed_args = parser.parse_args(args)

    local_path = parsed_args.local_path
    remote_path = parsed_args.remote_path if parsed_args.remote_path else local_path

    with _connect(env) as sftp:
        if os.path.isdir(local_path):
            sftp.put_d(local_path, remote_path)
        else:
            sftp.put(local_path, remote_path)
