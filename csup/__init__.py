#!/usr/bin/env python
import docker, requests, os, sys, json, time, argparse
from dateutil.parser import parse as dateparse
from requests.packages.urllib3 import Retry


__author__ = 'Steve McGrath <smcgrath@tenable.com>'
__version__ = '0.0.2'


class APIError(Exception):
    def __init__(self, code, msg):
        self.code = code
        self.msg = msg

    def __str__(self):
        return repr('[%s]: %s' % (self.code, self.msg))


class ContainerSecurity(object):
    _uri = 'https://cloud.tenable.com/container-security/api/v1'
    _registry = 'registry.cloud.tenable.com'
    _timer = 5
    _access_key = None
    _secret_key = None

    def __init__(self, access_key, secret_key, **kwargs):
        self._access_key = access_key
        self._secret_key = secret_key 

        # Sets the retry adaptor with the ability to properly backoff if we get 429s
        retries = Retry(
            total=3,
            status_forcelist={429, 501, 502, 503, 504}, 
            backoff_factor=0.1, 
            respect_retry_after_header=True
        )
        adapter = requests.adapters.HTTPAdapter(retries)

        # initiate the session and then attach the Retry adaptor.
        self._session = requests.Session()
        self._session.mount('https://', adapter)

        # we need to make sure to identify ourselves.
        self._session.headers.update({
            'User-Agent': 'CSUp/{} Python/{}'.format(__version__, '.'.join([str(i) for i in sys.version_info][0:3])),
            'X-APIKeys': 'accessKey={}; secretKey={};'.format(access_key, secret_key)
        })

        if 'uri' in kwargs and kwargs['uri']:
            self._uri = kwargs['uri']
        if 'registry' in kwargs and kwargs['registry']:
            self._registry = kwargs['registry']

    def _request(self, method, path, return_json=True, **kwargs):
        '''
        HTTP Request Function
        '''
        resp = self._session.request(method, '{}/{}'.format(self._uri, path), **kwargs)

        # If we aren't streaming the response back to the caller, then we should
        # check to see if the response is valid and raise an APIError exception
        # if an error is returned back.
        if 'stream' in kwargs and kwargs['stream']:
            return resp
        elif return_json:
            try:
                # Lets check for an error code in the returned JSON dictionary.
                # if one exists, then lets raise an exception.  Otherwise, we
                # should return the Python dictionary that we have already
                # computed out.
                return resp.json()
            except ValueError:
                # obviously something went wrong with the JSON parser, so lets
                # just return the response object.
                return resp
        else:
            # We were told not to return json data and not to stream, so lets just
            # return the response object
            return resp

    def _waiting(self, image_id):
        '''
        Wait for the job status of the image to either report completed or failed.
        '''
        done = False
        while not done:
            status = self.status(image_id)
            if 'job_status' in status:
                done = status['job_status'] in ['completed', 'failed']
            if not done:
                time.sleep(self._timer)
        return status['job_status'] == 'completed'

    def status(self, image_id):
        '''
        Return the Image Test Status
        '''
        return self._request('GET', 'jobs/image_status', params={
            'image_id': image_id
        })

    def policy(self, image_id, block=False):
        '''
        Return the Policy Compliance Status for the Image
        '''
        if block:
            if not self._waiting(image_id):
                return None
        return self._request('GET', 'policycompliance', params={
            'image_id': image_id
        })

    def report(self, image_id, block=False):
        '''
        Return the Test Results for the Image
        '''
        if block:
            if not self._waiting(image_id):
                return None
        return self._request('GET', 'reports/by_image', params={
            'image_id': image_id
        })

    def docker_upload(self, name, tag=None, cs_name=None, cs_tag=None):
        '''
        Uploads an image into Tenable.io Container Security
        '''
        if not cs_name:
            cs_name = 'consectool/{}'.format(name)
        if not cs_tag:
            if tag:
                cs_tag = tag
            else:
                cs_tag = 'latest'
        d = docker.from_env()
        image = d.images.get('{}:{}'.format(name, tag) if tag else name)
        repo = '{}/{}:{}'.format(self._registry, cs_name, cs_tag)
        image.tag(repo)
        d.images.push(repo, auth_config={
            'username': self._access_key,
            'password': self._secret_key
        })
        d.images.remove(repo)
        return image.id.split(':')[1][:12]


def main():
    '''
    Main Function
    '''
    # Global Arguments
    parser = argparse.ArgumentParser(description='''
    Container Security UPloading and reporting tool (CSUP) is a commandline tool 
    designed to interface into Tenable.io's Container Security toolset for the 
    purposes of uploading docker containers, fetching the image reports, and 
    checking the policy compliance and status of the image tests.

    The global arguments must come before the action and inform csup how to 
    communicate to Container Security.
    ''')
    subparsers = parser.add_subparsers(
        dest='action',
        title='valid actions',
        help='additional help available')
    parser.add_argument('--access-key', 
        dest='access', 
        help='Tenable.io API access key', 
        default=os.environ.get('TIO_ACCESS_KEY'))
    parser.add_argument('--secret-key', 
        dest='secret', 
        help='Tenable.io API secret sey', 
        default=os.environ.get('TIO_SECRET_KEY'))
    parser.add_argument('--consec-path', 
        dest='registry', 
        help='alternative Tenable.io registry address', 
        default=os.environ.get('TIO_CS_ADDRESS'))
    parser.add_argument('--tio-path', 
        dest='api', 
        help='alternative Tenable.io URI', 
        default=os.environ.get('CS_API'))

    # Upload Subparser options
    parser_upload = subparsers.add_parser('upload', description='''
    The upload action will upload a docker image to Tenable.io Container 
    Security and then if specified, will wait for the policy status and/or 
    report to return.  If either the policy returns a non-passing status or if 
    any of the report thresholds have been tripped, csup will return a 
    non-zero status code.
    ''')
    parser_upload.add_argument('id', 
        help='container image name or id')
    parser_upload.add_argument('-t', '--tag',
        dest='tag',
        help='container image tag')
    parser_upload.add_argument('-N', '--consec-name', 
        dest='consec_name', 
        help='ContainerSecurity repository & image path')
    parser_upload.add_argument('-T', '--consec-tag', 
        dest='consec_tag', 
        help='ContainerSecurity image tag', 
        default='latest')
    parser_upload.add_argument('-r', '--report',
        dest='report',
        help='output the test results when tests have completed',
        action='store_true')
    parser_upload.add_argument('-p', '--policy',
        dest='policy',
        help='output the compliance status when tests have completed',
        action='store_true')
    parser_upload.add_argument('--no-wait',
        dest='sleep',
        help=argparse.SUPPRESS,
        action='store_false')
    parser_upload.add_argument('-c', '--cvss',
        dest='cvss',
        help='CVSS Score greater than or equal to this value will return a non-zero exit',
        type=float,
        default=11)
    parser_upload.add_argument('-m', '--malware',
        dest='malware',
        help='if malware is discovered and this flag is set, a non-zero exit will occur',
        action='store_true')
    parser_upload.add_argument('-P', '--pups',
        dest='pup',
        help='if Potentially Unwanted Software is discovered and this flag is set, a non-zero exit will occur',
        action='store_true')
    parser_upload.add_argument('--json',
        dest='json',
        help='returns the data as a JSON object instead of formatted text',
        action='store_true')

    # Report Subparser options
    parser_report = subparsers.add_parser('report', description='''
    Retrieve a report for the image ID specified.  If any of the thresholds are
    set (and then met), then return a non-zero return status code.
    ''')
    parser_report.add_argument('id', 
        help='container image id')
    parser_report.add_argument('-w', '--wait', 
        dest='sleep', 
        help='wait for testing of the image to complete', 
        action='store_true')
    parser_report.add_argument('-c', '--cvss',
        dest='cvss',
        help='CVSS Score greater than or equal to this value will return a non-zero exit',
        type=float,
        default=11)
    parser_report.add_argument('-m', '--malware',
        dest='malware',
        help='if malware is discovered and this flag is set, a non-zero exit will occur',
        action='store_true')
    parser_report.add_argument('-P', '--pups',
        dest='pup',
        help='if Potentially Unwanted Software is discovered and this flag is set, a non-zero exit will occur',
        action='store_true')
    parser_report.add_argument('--json',
        dest='json',
        help='returns the data as a JSON object instead of formatted text',
        action='store_true')


    # Policy Subparser options
    parser_policy = subparsers.add_parser('policy', description='''
    Retrieve the policy status for the image ID specified.  If the policy
    compliance status is a failure, then return a non-zero status code.
    ''')
    parser_policy.add_argument('id', 
        help='container image id')
    parser_policy.add_argument('-w', '--wait', 
        dest='sleep', 
        help='wait for testing of the image to complete', 
        action='store_true')
    parser_policy.add_argument('--json',
        dest='json',
        help='returns the data as a JSON object instead of formatted text',
        action='store_true')

    # Status subparser options
    parser_status = subparsers.add_parser('status', description='''
    Get the current job status of the image ID specified.
    ''')
    parser_status.add_argument('id', 
        help='image id')
    parser_status.add_argument('--json',
        dest='json',
        help='returns the data as a JSON object instead of formatted text',
        action='store_true')

    # process the arguments and instantiate the ContainerSecurity object and
    # data dictionary.
    args = parser.parse_args()
    consec = ContainerSecurity(args.access, args.secret, uri=args.api, registry=args.registry)
    data = {}
    retval = 0

    # If the action was to upload an image, we want to make sure to do that first.
    if args.action == 'upload':
        ustart = time.time()
        data['upload'] = {
            'image_id': consec.docker_upload(args.id, tag=args.tag, cs_name=args.consec_name, cs_tag=args.consec_tag),
            'upload_time': int(time.time() - ustart)
        }

    # Now we want to make sure to use the appropriate image id.  If we have
    # uploaded an image, then we will want to use the returned image id form that
    # image, otherwise we want to use the image id that was provided as an argument
    if 'upload' in data and 'image_id' in data['upload']:
        image_id = data['upload']['image_id']
    else:
        image_id = args.id

    # Run the report, policy, and status actions if required.
    if args.action == 'report' or (args.action == 'upload' and args.report):
        data['report'] = consec.report(image_id, block=args.sleep)
    if args.action == 'policy' or (args.action == 'upload' and args.policy):
        data['policy'] = consec.policy(image_id, block=args.sleep)
    if args.action in ['status', 'upload', 'policy', 'report']:
        data['status'] = consec.status(image_id)


    if args.json:
        # If the User requested that the output be in JSON format, then we should
        # simply return a JSON formatted reponse from the data disctionary
        print json.dumps(data, sort_keys=True, indent=4)
    else:
        # Here we will attempt to interpret the output presented fromt he actions
        # above and present the data in a readable format for a commandline app.
        output = []
        if 'upload' in data and data['upload']:
            # Output the upload statistics if we had uploaded an image
            output.append('Uploaded Image: {}'.format(data['upload']['image_id']))
            output.append('Upload Time: {} seconds'.format(data['upload']['upload_time']))

        if 'status' in data and data['status']:
            # Output the status information about the image testing job(s).
            if data['status'] == 'not_found':
                output.append('Could not find Image')
            else:
                line = 'Test Status: {}'.format(data['status']['job_status'].upper())
                if data['status']['job_status'] == 'completed':
                    duration = (dateparse(data['status']['updated_at']) - dateparse(data['status']['created_at'])).seconds
                    line += ' in {} seconds'.format(duration)
                output.append(line)

        if 'policy' in data and data['policy']:
            # Output the policy compliance information about the image tested.
            if 'error' in data['policy'] and data['policy']['error'] in ['image not found', 'Internal Server Error']:
                # If we encountered an error, then we need to display the error and bail.
                output.append('Could not perform policy action: {}'.format(r['error']))
            else:
                output.append('Compliance Status: {}'.format(data['policy']['status'].upper()))

        if 'report' in data and data['report']:
            # Output the report information about the image tested.
            r = data['report']
            if 'error' in r and r['error'] in ['image not found', 'Internal Server Error']:
                # If we encountered an error, then we need to display the error and bail.
                output.append('Could not perform report action: {}'.format(r['error']))
            else:
                # some formatted output of most of the main fields (but not all)
                output.append('Docker Image Id: {}'.format(r['docker_image_id']))
                output.append('Image Name: {}:{}'.format(r['image_name'], r['tag']))
                output.append('SHA256 Hash: {}'.format(r['sha256']))
                output.append('Operating System: {}'.format(r['os']))
                output.append('OS Version: {}'.format(r['os_version']))
                output.append('Architecture: {}'.format(r['os_architecture']))
                output.append('Risk Score: {}'.format(r['risk_score']))
                output.append('Image Created: {}'.format(r['created_at']))
                output.append('Image Updated: {}'.format(r['updated_at']))

                # Iterate through all of the artifacts discovered and display them
                # in ARTIFACT=VERSION format, one per line.
                if len(r['installed_packages']) > 0:
                    output.append('Artifacts Discovered:')
                    for artifact in r['installed_packages']:
                        output.append('\t- {}={}'.format(artifact['name'], artifact['version']))

                # Iterate throug all of the findings discovered through testing
                # and display the CVSS Score, CVE, and artifacts impacted for each.
                # We may want to display more detail down the road, however it gets
                # quite wordy and I wanted to keep the overall output of the formatted
                # data pretty concise.
                if len(r['findings']) > 0:
                    output.append('Findings Discovered:')
                    for finding in r['findings']:
                        output.append('\t{} {} [{}]'.format(
                            finding['nvdFinding']['cvss_score'], 
                            finding['nvdFinding']['cve'],
                            ', '.join(['{}={}'.format(p['name'], p['version']) for p in finding['packages']])
                        ))

                # Iterate through all of the malware discovered and display them
                # in ARTIFACT=SHASUM format, one per line.
                if len(r['malware']) > 0:
                    output.append('Malware Discovered:')
                    for m in r['malware']:
                        output.append('\t- {}={}'.format(m['file'], m['sha256']))

                # Iterate through all of the PUPs discovered and display them
                # in ARTIFACT=SHASUM format, one per line.
                if len(r['potentially_unwanted_programs']) > 0:
                    output.append('Potentially Unwanted Programs Discovered:')
                    for i in r['potentially_unwanted_programs']:
                        output.append('\t- {}={}'.format(i['file'], i['sha256']))         

        # Output everything to STDOut.
        print '\n'.join(output)

    # Now we need to check to see if we need to return a exit code other than 0.
    # While this is likely more complex than necessary, if someone wanted to be
    # shwifty they could determine what classes of things caused the failure:
    #
    # - Policy Violation = 1
    # - CVSS Threshold Violation = 2
    # - Discovered Malware = 4
    # - Potentially Unwanted Programs = 8
    # 
    # Also as the value of no 2 should collide, you could actually determine any
    # combination of things that have failed.  For example 5 would equal malware
    # and a policy violation. 6 would equal CVSS scores and Malware.
    if ('policy' in data 
      and data['policy']
      and 'status' in data['policy'] 
      and data['policy']['status'] != 'pass'):
        # If we see anything in the policy status other than "pass", we will
        # increment the return value by 1
        retval += 1
    if 'report' in data and data['report']:
        if 'findings' in data['report']:
            # For each finding we find over the CVSS threshold, we will increment
            # the return value by 1.  This way we can actually keep track of the
            # number of CVSS violations discovered in the return code.
            failed = False
            for f in data['report']['findings']:
                if float(f['nvdFinding']['cvss_score']) >= args.cvss:
                    failed = True
            if failed:
                retval += 2
        if (args.malware 
          and 'malware' in data['report'] 
          and len(data['report']['malware']) > 0):
            # If any malware was discovered and the malware flag has been set,
            # we will increment the return value by 1000.
            retval += 4
        if (args.pup
          and 'potentially_unwanted_programs' in data['report'] 
          and len(data['report']['potentially_unwanted_programs']) > 0):
            retval += 8

    sys.exit(retval)


if __name__ == '__main__':
    main()