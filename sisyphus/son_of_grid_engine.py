# Author: Jan-Thorsten Peter <peter@cs.rwth-aachen.de>

import os
import subprocess

import time
import logging

import getpass  # used to get username
import math

from xml.dom import minidom
import xml.etree.cElementTree
from collections import defaultdict, namedtuple

import sisyphus.global_settings as gs
from sisyphus.engine import EngineBase
from sisyphus.global_settings import STATE_RUNNING, STATE_UNKNOWN, STATE_QUEUE, STATE_QUEUE_ERROR

ENGINE_NAME = 'sge'
TaskInfo = namedtuple('TaskInfo', ["job_id", "task_id", "state"])


def escape_name(name):
    return name.replace('/', '.')


def try_to_multiply(y, x, backup_value=None):
    """ Tries to convert y to float multiply it by x and convert it back
    to a rounded string.
    return backup_value if it fails
    return y if backup_value == None """

    try:
        return str(int(float(y) * x))
    except ValueError:
        if backup_value is None:
            return y
        else:
            return backup_value


class SonOfGridEngine(EngineBase):

    def __init__(self, default_rqmt, gateway=None, auto_clean_eqw=True):
        self._task_info_cache_last_update = 0
        self.gateway = gateway
        self.default_rqmt = default_rqmt
        self.auto_clean_eqw = auto_clean_eqw

    def system_call(self, command, send_to_stdin=None):
        if self.gateway:
            system_command = ['ssh', '-x', self.gateway] + [' '.join(['cd', os.getcwd(), '&&'] + command)]
        else:
            # no gateway given, skip ssh local
            system_command = command

        logging.debug('shell_cmd: %s' % ' '.join(system_command))
        p = subprocess.Popen(system_command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if send_to_stdin:
            send_to_stdin = send_to_stdin.encode()
        out, err = p.communicate(input=send_to_stdin, timeout=30)

        def fix_output(o):
            # split output and drop last empty line
            o = o.split(b'\n')
            if o[-1] != b'':
                print(o[-1])
                assert(False)
            return o[:-1]

        out = fix_output(out)
        err = fix_output(err)
        retval = p.wait(timeout=30)

        # Check for ssh error
        err_ = []
        for raw_line in err:
            lstart = 'ControlSocket'
            lend = 'already exists, disabling multiplexing'
            line = raw_line.decode('utf8').strip()
            if line.startswith(lstart) and line.endswith(lend):
                # found ssh connection problem
                ssh_file = line[len(lstart):len(lend)].strip()
                logging.warning('SSH Error %s' % line.strip())
                try:
                    os.unlink(ssh_file)
                    logging.info('Delete file %s' % ssh_file)
                except:
                    logging.warning('Could not delete %s' % ssh_file)
            else:
                err_.append(raw_line)

        return (out, err_, retval)

    def options(self, rqmt):
        out = []
        try:
            mem = "%iG" % math.ceil(float(rqmt['mem']))
        except ValueError:
            mem = rqmt['mem']
        # mem = try_to_multiply(s['mem'], 1024*1024*1024) # convert to Gigabyte if possible

        out.append('-l')
        out.append('h_vmem=%s' % mem)

        out.append('-l')

        if 'rss' in rqmt:
            try:
                rss = "%iG" % math.ceil(float(rqmt['rss']))
            except ValueError:
                rss = rqmt['rss']
            # rss = try_to_multiply(s['rss'], 1024*1024*1024) # convert to Gigabyte if possible
            out.append('h_rss=%s' % rss)
        else:
            out.append('h_rss=%s' % mem)

        out.append('-l')
        out.append('gpu=%s' % rqmt.get('gpu', 0))

        out.append('-l')
        out.append('num_proc=%s' % rqmt.get('cpu', 1))

        # Try to convert time to float, calculate minutes from it
        # and convert it back to an rounded string
        # If it fails use string directly
        task_time = try_to_multiply(rqmt['time'], 60 * 60)  # convert to seconds if possible

        out.append('-l')
        out.append('h_rt=%s' % task_time)
        qsub_args = rqmt.get('qsub_args', [])
        if isinstance(qsub_args, str):
            qsub_args = qsub_args.split()
        out += qsub_args
        return out

    def submit_call(self, call, logpath, rqmt, name, task_name, task_ids):
        if not task_ids:
            # skip empty list
            return

        submitted = []
        start_id, end_id, step_size = (None, None, None)
        for task_id in task_ids:
            if start_id is None:
                start_id = task_id
            elif end_id is None:
                end_id = task_id
                step_size = end_id - start_id
            elif task_id == end_id + step_size:
                end_id = task_id
            else:
                # this id doesn't fit pattern, this should only happen if only parts of the jobs are restarted
                job_id = self.submit_helper(call, logpath, rqmt, name, task_name, start_id, end_id, step_size)
                submitted.append((list(range(start_id, end_id, step_size)), job_id))
                start_id, end_id, step_size = (task_id, None, None)
        assert(start_id is not None)
        if end_id is None:
            end_id = start_id
            step_size = 1
        job_id = self.submit_helper(call, logpath, rqmt, name, task_name, start_id, end_id, step_size)
        submitted.append((list(range(start_id, end_id, step_size)), job_id))
        return (ENGINE_NAME, submitted)

    def submit_helper(self, call, logpath, rqmt, name, task_name, start_id, end_id, step_size):
        name = escape_name(name)
        qsub_call = [
            'qsub',
            '-cwd',
            '-N',
            name,
            '-j',
            'y',
            '-o',
            logpath,
            '-l',
            'h_fsize=50G',
            '-S',
            '/bin/bash',
            '-m',
            'n']
        qsub_call += self.options(rqmt)

        qsub_call += ['-t', '%i-%i:%i' % (start_id, end_id, step_size)]
        command = ' '.join(call) + '\n'
        try:
            out, err, retval = self.system_call(qsub_call, command)
        except subprocess.TimeoutExpired:
            logging.warning('SSH command timeout %s' % str(command))
            time.sleep(gs.WAIT_PERIOD_SSH_TIMEOUT)
            return self.submit_helper(call, logpath, rqmt, name, task_name, start_id, end_id, step_size)

        ref_output = ['Your', 'job-array', '("%s")' % name, 'has', 'been', 'submitted']
        ref_output = [i.encode() for i in ref_output]

        job_id = None
        if len(out) == 1:
            sout = out[0].split()
            if retval != 0 or len(err) > 0 or len(sout) != 7 or sout[0:2] + sout[3:] != ref_output:
                print(retval, len(err), len(sout), sout[0:2], sout[3:], ref_output)
                logging.error("Error to submit job")
                logging.error("QSUB command: %s" % ' '.join(qsub_call))
                for line in out:
                    logging.error("Output: %s" % line.decode())
                for line in err:
                    logging.error("Error: %s" % line.decode())
                # reset cache, after error
                self.reset_cache()
            else:
                sjob_id = sout[2].decode().split('.')
                assert(len(sjob_id) == 2)
                assert(sjob_id[1] == '%i-%i:%i' % (start_id, end_id, step_size))
                job_id = sjob_id[0]

                logging.info("Submitted with job_id: %s %s" % (job_id, name))
                for task_id in range(start_id, end_id, step_size):
                    self._task_info_cache[(name, task_id)].append((job_id, 'qw'))

                if False:  # for debugging
                    logging.warn("Boost job!")
                    subprocess.check_call(('qalter', '-p', '300', job_id))

        else:
            logging.error("Error to submit job, return value: %i" % retval)
            logging.error("QSUB command: %s" % ' '.join(qsub_call))
            for line in out:
                logging.error("Output: %s" % line.decode())
            for line in err:
                logging.error("Error: %s" % line.decode())

            # reset cache, after error
            self.reset_cache()
        return job_id

    def reset_cache(self):
        self._task_info_cache_last_update = -10

    def queue_state(self):
        """ Return s list with all currently running tasks in this queue """

        if time.time() - self._task_info_cache_last_update < 30:
            # use cached value
            return self._task_info_cache

        # get qstat output
        system_command = ['qstat', '-xml', '-u', getpass.getuser()]
        try:
            out, err, retval = self.system_call(system_command)
        except subprocess.TimeoutExpired:
            logging.warning('SSH command timeout %s' % str(system_command))
            time.sleep(gs.WAIT_PERIOD_SSH_TIMEOUT)
            return self.queue_state()

        xml_data = ''.join(i.decode('utf8') for i in out)

        # parse qstat output
        try:
            etree = xml.etree.cElementTree.fromstring(xml_data)
        except xml.etree.cElementTree.ParseError:
            logging.warning('qstat -xml parsing error, retrying\n'
                            'command: %s\n'
                            'stdout: %s\n'
                            'stderr: %s\n'
                            'return value: %s' % (system_command, out, err, retval))
            time.sleep(gs.WAIT_PERIOD_QSTAT_PARSING)
            return self.queue_state()

        task_infos = defaultdict(list)
        for job in etree.getiterator('job_list'):
            job_info = {}
            for attr in job:
                text = attr.text
                if text is not None:
                    text = text.strip()
                job_info[attr.tag] = text

            name = job_info['JB_name'].strip()
            state = job_info['state'].strip()
            tasks = job_info.get('tasks', None)
            number = job_info['JB_job_number'].strip()

            def parse_tasks(string):
                """ Return one task object for each listed task """

                if string is None:
                    # No task id
                    return [None]

                try:
                    # just one task id
                    return [int(string)]
                except ValueError:
                    pass

                if ',' in string:
                    # multiple task ids
                    tasks_list = []
                    for i in string.split(','):
                        tasks_list += parse_tasks(i)
                    return tasks_list

                if ':' in string:
                    # taks list
                    start_end, step_size = string.split(':')
                    start, end = start_end.split('-')
                    return list(range(int(start), int(end) + 1, int(step_size)))
                logging.warning("Can not parse task: %s : %s : %s" % (str(name), str(tasks), str(string)))
                return []

            for task in parse_tasks(tasks):
                task_infos[(name, task)].append((number, state))

        self._task_info_cache = task_infos
        self._task_info_cache_last_update = time.time()
        return task_infos

    def task_state(self, task, task_id):
        """ Return task state:
        'r' == STATE_RUNNING
        'qw' == STATE_QUEUE
        not found == STATE_UNKNOWN
        everything else == STATE_QUEUE_ERROR
        """

        name = task.task_name()
        name = escape_name(name)
        task_name = (name, task_id)
        queue_state = self.queue_state()
        qs = queue_state[task_name]

        # task name should be uniq
        if len(qs) > 1:
            logging.warning(
                'More then one matching SGE task, use first match < %s > matches: %s' %
                (str(task_name), str(qs)))

        if qs == []:
            return STATE_UNKNOWN
        state = qs[0][1]
        if state in ['r', 't']:
            return STATE_RUNNING
        elif state == 'qw':
            return STATE_QUEUE
        elif state == 'Eqw':
            if self.auto_clean_eqw:
                logging.info('Clean job in error state: %s, %s, %s' % (name, task_id, qs))
                self.system_call(['qmod', '-cj', "%s.%s" % (qs[0][0], task_id)])
            return STATE_QUEUE_ERROR
        else:
            return STATE_QUEUE_ERROR

    def start_engine(self):
        """ No starting action required with the current implementation """
        pass

    def stop_engine(self):
        """ No stopping action required with the current implementation """
        pass

    def get_task_id(self, task_id, engine_selector):
        assert task_id is None, "SGE task should not be started with task id, it's given via $SGE_TASK_ID"
        task_id = os.getenv('SGE_TASK_ID')
        if task_id in ['undefined', None]:
            # SGE without an array job
            logging.critical("Job started without task_id, this should not happen! Continue with task_id=1")
            return 1
        else:
            return int(task_id)

    def get_default_rqmt(self, task):
        return self.default_rqmt

    @staticmethod
    def get_logpath(logpath_base, task_name, task_id, engine_selector=None):
        """ Returns log file for the currently running task """
        return os.getenv('SGE_STDERR_PATH')