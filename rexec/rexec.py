#!/usr/bin/env python3
import os, sys, argparse, subprocess, time, traceback, json, getpass
#
# the BDFL does not admire scripts which are also importable modules
# well, frack him -- this is how we roll
#
#so absolute imports work in script mode, we need to import from the parent folder
opath = os.path.abspath(".")
abspath = os.path.abspath(__file__)
abspath = abspath[:abspath.rfind('/') + 1]
os.chdir(abspath)
abspath = os.path.abspath("..")
sys.path.insert(0, abspath)

from rexec.lcloud import *
from rexec.runrun import run
from rexec.version import version

os.chdir(opath)

DEFAULT_IMAGE = "rexec_image" #FIXME: should be unique to folder structure
SHUTDOWN_IMAGE = "datahubdock/rexec:rexec_shutdown"
DOCKER_REMPORT = "2376"
DOCKER_REMOTE = "localhost:"+DOCKER_REMPORT

def rexec(args, sshuser=None, url=None, uuid=None, rxuser=None, gpus = "", ports=None, stop=False,
          image=None, size=None, pubkey=None, dockerfile="Dockerfile",
          cloudmap="", conf=None):
    tunnel = None
    try:
        if url:
            if not sshuser:
                sshuser, url = url.split('@')
        node = None
        if url or uuid or rxuser:
            node = get_server(url=url, uuid=uuid, name=rxuser, conf=conf)
            if rxuser and not node:
                node = launch_server(rxuser, pubkey=pubkey, size=size, image=image, conf=conf, user=sshuser, gpus=gpus)
            if node:
                if node.state.lower() != "running":
                    print ("Starting server")
                    node = start_server(node)
                url = node.public_ips[0]
                print ("Waiting for sshd")
                cmd = ["ssh", "-o StrictHostKeyChecking=no", "-o UserKnownHostsFile=/dev/null", "{0}@{1}".format(sshuser, url), "echo", "'sshd responding'"]
                print(cmd)
                good = False
                for z in range(10, -1, -1):
                    ret = run(cmd, timeout=15)
                    if ret[0].strip()[-15:]=='sshd responding':
                        good = True
                        break
                    print ("sshd not responding; %d attempts left" % z)
                    if z:
                        time.sleep(5)
                if not good:
                    raise Exception("error in ssh call: %s" % ret[0].strip())
                print ("SSH returns -->%s|%s<--" % ret)
            else:
                raise Exception("Error: node not found")

        if url:
            remote = "-H " + DOCKER_REMOTE
            ssh_args = ["ssh", "-o StrictHostKeyChecking=no", "-o UserKnownHostsFile=/dev/null", "-NL", "{0}:/var/run/docker.sock".format(DOCKER_REMPORT), "{0}@{1}".format(sshuser, url)]
            print (ssh_args)
            tunnel = subprocess.Popen(ssh_args)
            time.sleep(5)
            relpath = os.path.abspath('.')[len(os.path.expanduser('~')):]
            relpath = "/_REXEC" +  relpath.replace('/', '_') #I can exlain
            locpath = os.path.abspath('.')
            path = "/home/{0}{1}".format(sshuser, relpath)

            cmd = ["docker", "{0}".format(remote), "ps", "--format", '{{json .}}']
            print (cmd)
            out = run(cmd)
            print("PS returns -->%s|%s<--" % out)
            if out[0].strip():
                kills = []
                for x in out[0].split("\n"):
                    if x:
                        j = json.loads(x)
                        Command = j['Command']
                        if Command.find("rexec --stop") <2:
                            kills.append(j['ID'])
                if kills:
                    print ("Killing shutdown processes:", kills)
                    cmd = "docker {0} stop {1} > /dev/null &".format(remote, " ".join(kills))
                    print (cmd)
                    os.system(cmd)
            print ("Removing topmost layer")        #to avoid running stale image
            cmd = ["docker", "{0}".format(remote), "rmi", "--no-prune", DEFAULT_IMAGE]
            print (cmd)
            out, err = run(cmd)
            print (out)
            # print ("DEEBG ex:", node.extra)
            size, image = fix_size_and_image(size, image)
            if size and size != get_server_size(node):
                raise Exception("FIXME: cannot change size (EC2 instance type) -- need to re-launch")
            if image and image != get_server_image(node):
                raise Exception("FIXME: cannot change image (EC2 ami) -- need to terminate & re-launch server")
            print ("rexec: name %s size %s image %s url %s" % (node.name, size, image, url))

            #sync project directory
            cmd = 'rsync -vrltzu -e "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null" {0}/* {3}@{1}:{2}/'.format(locpath, url, path, sshuser)
            print (cmd)
            os.system(cmd)
            if get_config().provider == 'GCE':
                # sync service acct creds (for shutdown)
                cmd = 'rsync -vrltzu --relative -e "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null" {0}/./.rexec/gce_srv_privkey.json {3}@{1}:{2}/'.format(os.path.expanduser('~'), url, path, sshuser)
                print (cmd)
                os.system(cmd)
        else:
            print ("rexec: running locally")
            remote = ""
            path = os.path.abspath('.')


        cmd = "docker {1} build . --file {2} -t {0}".format(DEFAULT_IMAGE, remote, dockerfile)
        print (cmd)
        os.system(cmd)

        args = " ".join(args)
        gpu_args = "--gpus "+gpus if gpus else ""
        port_args = ""
        if ports:
            for pa in ports:
                if ':' not in pa:
                    pa = "{0}:{0}".format(pa)
                port_args += " -p " + pa

        cloud_args = ""
        if cloudmap:
            if remote:
                local_rcred = f"{os.environ['HOME']}/.rexec"
                rcred = " /home/ubuntu/.rexec/"
                cmd = f'rsync -vrltzu  -e "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null" {local_rcred}/rclone.conf {sshuser}@{url}:{rcred}'
                print(cmd)
                os.system(cmd)
            else:
                rcred = f"{os.environ['HOME']}/.rexec"

            cloud_args = f"-v {rcred}:/root/.config/rclone --privileged"
            cloud, host = cloudmap.split(":")
            args = f"bash -c 'mkdir -p {host}; rclone mount {cloud}: {host} & sleep 3; {args}; umount {host}'"

        cmd = "docker {3} run {4} {5} --rm -ti -v {2}:/home/rexec/work {6} {0} {1}".format(DEFAULT_IMAGE,
                                                                                  args, path, remote, gpu_args, port_args, cloud_args)
        print (cmd)
        print ("\n\n---------------------OUTPUT-----------------------")
        os.system(cmd)
        print ("----------------------END-------------------------\n\n")
        if url:
            cmd = "rsync -vrltzu  -e 'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null' '{3}@{1}:{2}/*' {0}/".format(locpath, url, path, sshuser)
            print (cmd)
            os.system(cmd)
    except:
        traceback.print_exc()
        print ("--------------------------------")

    if url and node:
        if stop == 0:
            print ("Stopping VM at %s immediately as instructed" % url)
            stop_server(node)
        else:
            print ("Scheduling shutdown of VM at %s for %d seconds from now" % (url, stop))
            conf = get_config()
            secret = conf.secret
            # hack to look for GCE service acct key in local dir on container
            if  conf.provider == 'GCE' and secret[-5:]==".json" and secret[0:2] == '~/': #the things we do
                secret = "./" + secret[2:]
            cmd = "docker {7} run --rm -d -v {9}:/home/rexec/work {8} rexec --stop_instance_by_url {0} --delay={1} --access={2} --secret={3} --region={4} {5} --provider={6}".format(url,
                                                                    stop, conf.access, secret, conf.region,
                                                                    ("--project=" + conf.project) if conf.project else "",
                                                                    conf.provider,
                                                                    remote, SHUTDOWN_IMAGE, path)
            # cmd = "docker {0} run --rm -ti {1} rexec --version".format(remote, DEFAULT_IMAGE)
            print (cmd[:1000] + "...")
            print ("Shutdown process container ID:")
            os.system(cmd)

    if tunnel:
        tunnel.kill()

#
# Note this function is typically called by the shutdown process so it does
# not share scope with most of what rexec does
#
def stop_instance_by_url(url, conf):
    print ("STOP instance with public IP", url)
    # print ("DEBUG", os.path.abspath('.'), conf.secret)
    node = get_server(url=url, conf=conf)
    if not node:
        print ("No active instance found for IP", url)
    else:
        print ("shutting down node %s" % node)
        stop_server(node)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs='?',                   help="Command to run on remote server")
    parser.add_argument("--sshuser", default="ubuntu",          help="remote server username")
    parser.add_argument("--local", action="store_true",         help="run on local device")
    parser.add_argument("--list-servers", action="store_true",  help="List all associated remote servers")
    parser.add_argument("--terminate-servers", action="store_true",     help="Terminate associated remote servers")
    parser.add_argument("--version", action="store_true",       help="Print version # & exit")
    parser.add_argument("--url",                                help="run on remote server specified by url")
    parser.add_argument("--uuid",                               help="run on remote server specified by libcloud uuid")
    parser.add_argument("--rexecuser",                          help="Rexec user name; defaults to local username")
    parser.add_argument("--gpus",                               help="docker run gpu option (usually 'all')")
    parser.add_argument("-p", action="append",                  help="docker port mapping")
    parser.add_argument("--access",                             help="libcloud username (aws: ACCESS_KEY)")
    parser.add_argument("--secret",                             help="libcloud password (aws: SECRET)")
    parser.add_argument("--region",                             help="libcloud location (aws: region)")
    parser.add_argument("--project",                            help="GCE project ID")
    parser.add_argument("--provider", default='EC2',            help="GCE, EC2 etc.")
    parser.add_argument("--image",                              help="libcloud image (aws: ami image_id")
    parser.add_argument("--size",                               help="libcloud size (aws: instance_type")
    parser.add_argument("--pubkey",                             help="public key to access server (defaults to ~/.ssh/id_rsa.pub)")
    parser.add_argument("--delay", type=int, default=0,         help="delay command by N seconds")
    parser.add_argument("--shutdown", type=int, default=900, nargs='?',   help="seconds before server is stopped (default 15 minutes)")
    parser.add_argument("--stop_instance_by_url",               help="internal use")
    parser.add_argument("--dockerfile", type=str, default="Dockerfile",    help="Docker file to build the container with if not ./Dockerfile")
    parser.add_argument("--cloudmap", type=str, default="",     help="map cloud storage to local mount point")

    if len(sys.argv) < 2:
        parser.print_usage()
        sys.exit(1)
    #
    # this got a bit tricky.
    # we want to parse args BEFORE the main command as rexec options
    # and pass all args AFTER the main command to the command when it runs remotely
    #
    argv = sys.argv[1:]
    print ("ARGV:", argv)
    args, unknown = parser.parse_known_args(argv)
    if args.command != None:
        i = argv.index(args.command)
    else:
        i = len(argv)
    rexargs = argv[:i]
    print ("REXARGS:", rexargs)
    cmdargs = argv[i:]
    print ("CMDARGS:", cmdargs)
    args = parser.parse_args(rexargs)
    print ("ARGS:", args)

    if args.access:
        args_conf = dictobj()
        args_conf.access = args.access
        args_conf.secret = args.secret
        args_conf.region = args.region
        args_conf.project = args.project
        args_conf.provider = args.provider
    else:
        args_conf = None

    if args.local and (args.uuid or args.url):
        print (args)
        parser.error("when specifying --local, do not set --sshuser, --rexecuser, --uuid, or --url")
        exit()
    t0 = time.time()
    while time.time()-t0 < args.delay:
        print ("%d seconds till action" % (args.delay+.5+t0-time.time()))
        time.sleep(5)

    if not (args.rexecuser or args.uuid or args.url or args.local):
        rxuser = getpass.getuser()
        args.rexecuser = "rexec-" + rxuser
        print ("Rexec virtual machine name:", args.rexecuser)

    if args.stop_instance_by_url:
        stop_instance_by_url(args.stop_instance_by_url, args_conf)

    elif args.list_servers:     #note this is different than --shutdown 0 -- we just shut down without running
        print ("-------------------------------------------------------------\nSERVERS associated with %s:" % args.rexecuser)
        for s in list_servers(args.rexecuser, args_conf):
            print (s)
        print ("-------------------------------------------------------------")

    elif args.shutdown == None:
        print ("-------------------------------------------------------------")
        for s in list_servers(args.rexecuser, args_conf):
            yes = input("Stopping (warm shutdown) %s %s are you sure?" % (s.name, s.public_ips))
            if yes=='y':
                stop_server(s)
            else:
                print ("Aborted")
        print ("-------------------------------------------------------------")

    elif args.terminate_servers:
        print ("-------------------------------------------------------------")
        for s in list_servers(args.rexecuser, args_conf):
            yes = input("Terminating %s %s are you sure?" % (s.name, s.public_ips))
            if yes=='y':
                terminate_server(s)
            else:
                print ("Aborted")
        print ("-------------------------------------------------------------")

    elif args.version:
        print ("\nVERSION:", version)

    else:

        if args.pubkey==None:
            try:
                f=open(os.path.expanduser("~") + "/.ssh/id_rsa.pub")             #FIXME: a bit cheeky
                pubkey=f.read()
                f.close()
            except:
                print ("Public key not found in usual place; please specify --pubkey")
        if args.gpus:
            if args.size == None:
                size = 'DEFAULT_GPU_SIZE'
            else:
                size = args.size
            if args.image == None:
                image = 'DEFAULT_GPU_IMAGE'
            else:
                image = args.image
        else:
            if args.size == None:
                size = 'DEFAULT_SIZE'
            else:
                size = args.size
            if args.image == None:
                image = 'DEFAULT_IMAGE'
            else:
                image = args.image

        rexec(cmdargs, sshuser=args.sshuser, url=args.url, uuid=args.uuid,
              rxuser=args.rexecuser, gpus=args.gpus, ports=args.p, stop=args.shutdown,
              image=image, size=size, pubkey=pubkey, dockerfile=args.dockerfile, cloudmap=args.cloudmap,
              conf = args_conf)
        print ("DONE")
