# EKS Auto Drain

Gracefully drain EKS Worker Nodes whenever a node is terminated by an Auto Scaling Group or a Spot termination.

Deployable Lambda function with CloudWatch Event Rules and an IAM Role, enabled by adding a Lifecycle hook to any Auto Scaling Group in the same AWS Region.

## Deploy

Deployment of the Lambda, IAM Role and Cloudwatch Event Rules can be simplified with the SAM CLI and Docker.

[SAM CLI can be installed](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/serverless-sam-cli-install.html) differently depending on your OS, in general for Linux and MacOS however..

```bash
brew upgrade
brew update
brew tap aws/tap
brew install aws-sam-cli
sam --version
```

### Build, Package, and Deploy using SAM

* Clone this repository

```bash
git clone https://github.com/dkeightley/eks-auto-drain.git
cd eks-auto-drain
```

* Optional: set your AWS region and create an S3 bucket

```bash
export AWS_DEFAULT_REGION=<region name>
aws s3 mb s3://<bucket name>
```

* Build, package and deploy the project with SAM

```bash
sam build --use-container
sam package --output-template-file packaged.yaml --s3-bucket <bucket name>
sam deploy --template-file packaged.yaml --stack-name eks-auto-drain --capabilities CAPABILITY_IAM
```

## Configure

To provide RBAC permissions for the drain, an RBAC group that provides the specific permissions is needed. Once added the Lambda execution role can be mapped to the group in the `aws-auth` ConfigMap for each EKS Cluster

* Deploy the RBAC ClusterRole and ClusterRoleBinding for each Cluster

```bash
kubectl apply -f rbac/
```

* Obtain the Lambda execution Role

```bash
aws cloudformation describe-stacks --stack-name eks-auto-drain --query 'Stacks[0].Outputs[0].OutputValue'
 ```

* Add a mapping for the Role to the ClusterRole for each Cluster
\
Use an imperative action, like edit, to add to the ConfigMap to avoid merge conflicts

```bash
kubectl edit -n kube-system configmap aws-auth
```

Example:

```yaml
mapRoles: |
    - groups:
      - eks-auto-drain-lambda
      rolearn: <Lambda execution Role>
      username: eks-auto-drain-lambda
```

* Add a Lifecycle hook to each Auto Scaling Group for the Nodes in each Cluster

Define a variable to loop through these, otherwise the below command can be used for each ASG

`aws autoscaling put-lifecycle-hook --lifecycle-hook-name eks-auto-drain --lifecycle-transition "autoscaling:EC2_INSTANCE_TERMINATING" --heartbeat-timeout 300 --default-result CONTINUE --auto-scaling-group-name <auto scaling group name>`

```bash
ASGS="group-1 group-2 group-3"
```

```bash
for i in $ASGS
  do
    aws autoscaling put-lifecycle-hook --lifecycle-hook-name eks-auto-drain --lifecycle-transition "autoscaling:EC2_INSTANCE_TERMINATING" --heartbeat-timeout 300 --default-result CONTINUE --auto-scaling-group-name $i
done
```

# Test

### Testing by terminating an instance

Obtain a list of instances in a Cluster:

`kubectl get nodes -o=custom-columns=NAME:.metadata.name,INSTANCE:.spec.providerID `

Test by terminating an instance in an ASG

```bash
aws autoscaling terminate-instance-in-auto-scaling-group --no-should-decrement-desired-capacity --instance-id <instance id>
```

The Node should be cordoned, and drained of all Pods before termination. The Lambda function logs can provide output.

```bash
sam logs --name LambdaFunction --stack-name eks-auto-drain
```

### Local testing with the SAM CLI

The provided event.json contains an invalid instance id so will fail, however, replace with a valid instance from your cluster to ensure the drain occurs

```bash
sam local invoke -e misc/event.json
```

## Cleanup

```bash
kubectl delete -f rbac/
aws cloudformation delete-stack --stack-name eks-auto-drain
```

```bash
ASGS="group-1 group-2 group-3
for i in $ASGS
  do
    aws autoscaling delete-lifecycle-hook --lifecycle-hook-name eks-auto-drain  --auto-scaling-group-name $i
done
```

## TODO

* VPC support for private access