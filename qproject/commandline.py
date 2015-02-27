import sys
import argparse
import logging
import logging.handlers
import os
import itertools
import json
import shutil
from . import projects, utils


logger = logging.getLogger(__name__)


def init_logging(jobid=None, name=None, daemon=True):
    if daemon:
        handler = logging.handlers.SysLogHandler('/dev/log')
    else:
        handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)

    if jobid is not None:
        formatter = logging.Formatter(
            'qproject - job {} - %(message)s'.format(jobid)
        )
    elif name is not None:
        formatter = logging.Formatter(
            'qproject - job {} - %(message)s'.format(name)
        )
    else:
        formatter = logging.Formatter('qproject - %(message)s')

    handler.setFormatter(formatter)

    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    for module in ['qproject.utils', 'qproject.projects']:
        module_logger = logging.getLogger(module)
        module_logger.setLevel(logging.DEBUG)
        module_logger.addHandler(handler)


def parse_args():
    parser = argparse.ArgumentParser(
        description='Download data from OpenBIS and prepare working directory'
    )

    parser.add_argument('command', choices=['prepare', 'run', 'commit'])
    parser.add_argument('--target', '-t',
                        help='Base directory where the files should be stored',
                        required=True)
    parser.add_argument('--workflow', '-w', nargs='+',
                        help='Checkout a workflow from this git repository',
                        default=[])
    parser.add_argument('--commit', '-c', nargs='+',
                        help="Commits of the workflows.", default=[])
    parser.add_argument('--data', help='Input files to copy to workdir',
                        nargs='*', default=[])
    parser.add_argument('--params', '-p', nargs='+',
                        help='Parameter file for each specified workflow',
                        default=[])
    parser.add_argument('--user', '-u', help='User name for execution of '
                        'workflow. ACL will be set so this user can access '
                        'input files and write to result and var')
    parser.add_argument('--group', '-g', help="Add read and write permissions "
                        "to the project directory to all members of this "
                        "unix group")
    parser.add_argument('--jobid', help="A jobid at a workflow server. "
                        "Status update will be sent to this server")
    parser.add_argument('--server-file', help="Path to a file that contains "
                        "the address of a workflow server and a password. "
                        "Requires jobid.")
    parser.add_argument('--dropbox', help="Write results to this dir")
    parser.add_argument('--barcode', help='barcode for dropbox')
    parser.add_argument('--daemon', '-d', help="Daemonize qproject",
                        action="store_true", default=False)
    parser.add_argument('--pidfile', help="Path to pidfile")
    parser.add_argument('--umask', help="Umask for files in workdir",
                        default=0o077)
    parser.add_argument('--cleanup', help='Delete workdir when finished',
                        default=False, action='store_true')
    return parser.parse_args()


def validate_args(args):
    if args.dropbox:
        if not args.barcode:
            raise ValueError("barcode must be specified if dropbox is")
        if not args.user:
            raise ValueError("specify user to copy back data")
    if args.daemon and not args.pidfile:
        raise ValueError("pidfile must be specified if daemon is")
    if args.daemon:
        if os.path.exists(args.pidfile):
            raise ValueError("Pidfile exists: %s" % args.pidfile)
        if not os.path.isdir(os.path.dirname(args.pidfile)):
            raise ValueError("Invalid pidfile: %s" % args.pidfile)
    if args.command in ['run', 'commit'] and not args.dropbox:
        raise ValueError("dropbox must be specified for command '%s'" %
                         args.command)


def prepare_command(args, clone=True, copy_data=True):
    workspace = projects.prepare(
        args.target, True, user=args.user, group=args.group
    )
    workflows = []
    for remote, commit, params in itertools.zip_longest(
            args.workflow, args.commit, args.params):
        if params:
            with open(params) as f:
                try:
                    params = json.load(f)
                except ValueError:
                    logger.exception("Invalid parameter file: %s" % params)
                    raise
        else:
            params = None
        workflow = projects.Workflow(
            workspace, remote=remote, commit=commit, params=params
        )
        if clone:
            workflow.create(user=args.user, group=args.group)
            workflow.clone()
            workflow.write_config(args.user, args.group)
        workflows.append(workflow)

    if args.data and copy_data:
        projects.copy_data(workspace, args.data, args.user, args.group)
    return workspace, workflows


def run_command(args):
    workspace, workflows = prepare_command(args, clone=False, copy_data=False)

    def run():
        try:
            if args.data:
                projects.copy_data(workspace, args.data, args.user, args.group)
            for workflow in workflows:
                workflow.create()
                workflow.clone()
                workflow.write_config(args.user, args.group)
                popen = workflow.run(user=args.user)
                popen.wait()
                if popen.returncode:
                    raise RuntimeError(
                        "Workflow %s return non-zero returncode. See "
                        "workflow log for details" % workflow.name
                    )
                else:
                    logger.info("Workflow %s successfull." % workflow.name)
        except Exception:
            logger.exception("Got exception while executing workflows:")
        else:
            logger.info("Workflows were executed successfully")
        finally:
            commit_command(args)

    if args.daemon:
        utils.daemonize(run, args.pidfile, args.umask)
    else:
        os.umask(args.umask)
        run()


def commit_command(args):
    try:
        logger.info("Write results and logs to dropbox")
        workspace = projects.prepare(args.target, False)

        if args.barcode:
            dropbox = os.path.join(args.dropbox, args.barcode)
        else:
            dropbox = os.path.join(args.dropbox, args.jobid)

        if os.path.exists(dropbox):
            raise ValueError(
                "Dropbox directory exists: %s. Could not copy results "
                "The Workspace will *not* be cleaned up: %s" %
                (dropbox, workspace.base)
            )

        names = os.listdir(workspace.src)
        workflows = [projects.Workflow(workspace, name) for name in names]

        for workflow in workflows:
            workflow.commit(dropbox, args.user, umask=0o077)
        if args.cleanup:
            logger.info("Removing workspace")
            shutil.rmtree(workspace.base)
    except:
        logger.critical("Failed to write results to dropbox.")
        raise


def main():
    retcode = 1
    try:
        args = parse_args()

        init_logging(args.jobid, args.target, args.daemon)

        validate_args(args)
        logger.info(
            "Starting qproject for user %s with command '%s' and target '%s'",
            args.user, args.command, args.target
        )
        if args.command == 'prepare':
            prepare_command(args)
        elif args.command == 'run':
            run_command(args)
        elif args.command == 'commit':
            commit_command(args)
        logger.info('qproject finished succesfully')
        retcode = 0
    except Exception:
        logger.exception("Failed to run qproject:")
    finally:
        logger.info("Exiting qproject")
        sys.exit(retcode)

if __name__ == '__main__':
    main()
