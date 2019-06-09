#!/bin/bash

if [ -n $1 ]
  then
    for ASG in "$@"
      do
        echo "Working with $ASG"
        aws autoscaling put-lifecycle-hook --lifecycle-hook-name eks-auto-drain --lifecycle-transition "autoscaling:EC2_INSTANCE_TERMINATING" --heartbeat-timeout 300 --default-result CONTINUE --auto-scaling-group-name $ASG
    done
fi