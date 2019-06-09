#!/bin/bash

if [ -n $1 ]
  then
    for ASG in "$@"
      do
        echo "Working with $ASG"
        aws autoscaling delete-lifecycle-hook --lifecycle-hook-name eks-auto-drain --auto-scaling-group-name $ASG
    done
fi