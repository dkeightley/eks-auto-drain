import base64
import boto3
import re
import yaml
import logging
import os
import sys
import time
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from botocore.signers import RequestSigner
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Optional grace period override when deleting Pods, after which a SIGKILL is sent [seconds]. Default is 30s, or the Pod value configured
# grace_period = 30
# Optional delay before signalling to the ASG lifecycle [seconds]
delay = 10

region = os.environ['AWS_DEFAULT_REGION']
cluster_state = '/tmp/cluster_name'
kubeconfig = '/tmp/kubeconfig'

def process_lifecycle(detail):

    same_cluster = False
    cluster_name = None
    instance = detail["EC2InstanceId"]
    ec2 = boto3.client('ec2', region_name=region)

    # Describe the instance so we can define the node_name and cluster_name from the response
    logger.info('Received event for {} in {}'.format(instance, region))
    try:
        instance_describe = ec2.describe_instances(InstanceIds=[instance])
    except ClientError as err:
        logger.exception('Problem describing instance {}'.format(instance))
        logger.exception(err.response)
        sys.exit(1)

    # Define node_name and cluster_name
    node_name = instance_describe["Reservations"][0]["Instances"][0]["PrivateDnsName"]
    for tags in instance_describe["Reservations"][0]["Instances"][0]["Tags"]:

        if tags["Key"] == 'KubernetesCluster':
            cluster_name = tags["Value"]
        
    if cluster_name is None:
        logger.exception('This instance doesn\'t appear to have a KubernetesCluster tag, exiting...')
        sys.exit(0)
    
    logger.info('Processing event for {} in the {} cluster'.format(node_name, cluster_name))

    # If the same cluster_name is configured ignore creating the kubeconfig
    if os.path.exists(cluster_state):
        with open(cluster_state) as state:
            contents = state.read()
            search_word = cluster_name

            if search_word in contents:
                logger.info('We are working with the same cluster, skipping the kubeconfig build')
                same_cluster = True
   
    if same_cluster is not True:

        if os.path.exists(cluster_state):
            os.remove(cluster_state)
       
        capture_state = open(cluster_state, "w")
        capture_state.write(cluster_name)
        capture_state.close()

        if os.path.exists(kubeconfig):
            os.remove(kubeconfig)

        logger.info('Building the kubeconfig file for {}'.format(cluster_name))
        create_kubeconfig(cluster_name)

    logger.info('Generating bearer token')
    token = get_bearer_token(cluster_name)

    # Configure the kubenetes client
    config.load_kube_config(kubeconfig)
    configuration = client.Configuration()
    configuration.api_key['authorization'] = token
    configuration.api_key_prefix['authorization'] = 'Bearer'

    # Configure the API 
    api = client.ApiClient(configuration)
    v1 = client.CoreV1Api(api)

    try:
        if not node_exists(v1, node_name):
            # Don't delay if the node already doesn't exist
            global delay
            del delay
            complete_lifecycle(detail) 
            return

        # Cordon the node, sleep for 2s remove all pods and complete the lifecycle
        cordon_node(v1, node_name)
        time.sleep(2)
        remove_all_pods(v1, node_name)
        complete_lifecycle(detail)

    except ApiException:
        logger.exception('There was an error removing the Pods from the Node {}'.format(node_name))
        complete_lifecycle(detail) 

def create_kubeconfig(cluster_name):
        
    kube_content = dict()
    # Get data from EKS API
    eks_api = boto3.client('eks')
    cluster_info = eks_api.describe_cluster(name=cluster_name)
    certificate = cluster_info['cluster']['certificateAuthority']['data']
    endpoint = cluster_info['cluster']['endpoint']

    # Generating kubeconfig
    kube_content = dict()
    
    kube_content['apiVersion'] = 'v1'
    kube_content['clusters'] = [
        {
        'cluster':
            {
            'server': endpoint,
            'certificate-authority-data': certificate
            },
        'name':'kubernetes'
                
        }]

    kube_content['contexts'] = [
        {
        'context':
            {
            'cluster':'kubernetes',
            'user':'aws'
            },
        'name':'aws'
        }]

    kube_content['current-context'] = 'aws'
    kube_content['Kind'] = 'config'
    kube_content['users'] = [
    {
    'name':'aws',
    'user':'lambda'
    }]

    # Write the kubeconfig
    with open(kubeconfig, 'w') as outfile:
        yaml.dump(kube_content, outfile, default_flow_style=False)

def get_bearer_token(cluster_name):

    STS_TOKEN_EXPIRES_IN = 60
    session = boto3.session.Session()

    client = session.client('sts')
    service_id = client.meta.service_model.service_id

    signer = RequestSigner(
        service_id,
        region,
        'sts',
        'v4',
        session.get_credentials(),
        session.events
    )

    params = {
        'method': 'GET',
        'url': 'https://sts.{}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15'.format(region),
        'body': {},
        'headers': {
            'x-k8s-aws-id': cluster_name
        },
        'context': {}
    }

    signed_url = signer.generate_presigned_url(
        params,
        region_name=region,
        expires_in=STS_TOKEN_EXPIRES_IN,
        operation_name=''
    )

    base64_url = base64.urlsafe_b64encode(signed_url.encode('utf-8')).decode('utf-8')

    # remove any base64 encoding padding:
    return 'k8s-aws-v1.' + re.sub(r'=*', '', base64_url)

def cordon_node(api, node_name):
    # Marks the specified node as unschedulable, which means that no new pods can be launched on the 
    # node by the Kubernetes scheduler
    patch_body = {
        'apiVersion': 'v1',
        'kind': 'Node',
        'metadata': {
            'name': node_name
        },
        'spec': {
            'unschedulable': True
        }
    }

    api.patch_node(node_name, patch_body)

def remove_all_pods(api, node_name):
    # Removes all Kubernetes pods from the specified node
    field_selector = 'spec.nodeName=' + node_name
    pods = api.list_pod_for_all_namespaces(watch=False, field_selector=field_selector)

    logger.debug('Number of pods to delete: ' + str(len(pods.items)))

    for pod in pods.items:
        logger.info('Deleting pod {} in namespace {}'.format(pod.metadata.name, pod.metadata.namespace))
        if 'grace_period' in globals():
            body = {
                'apiVersion': 'policy/v1beta1',
                'kind': 'Eviction',
                'metadata': {
                    'name': pod.metadata.name,
                    'namespace': pod.metadata.namespace,
                    'grace_period_seconds': grace_period
                }
            }
        else:
            body = {
                'apiVersion': 'policy/v1beta1',
                'kind': 'Eviction',
                'metadata': {
                    'name': pod.metadata.name,
                    'namespace': pod.metadata.namespace
                }
            }
        api.create_namespaced_pod_eviction(pod.metadata.name + '-eviction', pod.metadata.namespace, body)

def node_exists(api, node_name):
    # Determines whether the specified node is still part of the cluster
    nodes = api.list_node(include_uninitialized=True, pretty=True).items
    node = next((n for n in nodes if n.metadata.name == node_name), None)
    return False if not node else True

def complete_lifecycle(detail):

    if 'delay' in globals():
        # Sleep if delay is defined, then complete the ASG lifecycle (terminate the node)
        time.sleep(delay)

    logger.info('Completing the ASG lifecycle action')
    asg = boto3.client('autoscaling', region_name=region)
    asg.complete_lifecycle_action(
        LifecycleHookName=detail["LifecycleHookName"],
        AutoScalingGroupName=detail["AutoScalingGroupName"],
        LifecycleActionToken=detail["LifecycleActionToken"],
        LifecycleActionResult="ABANDON",
        InstanceId=detail["EC2InstanceId"]
    )

def lambda_handler(event, context):
    
    detail = event["detail"]
    process_lifecycle(detail)