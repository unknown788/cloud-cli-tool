"""
providers/aws_provider.py

AWS EC2 stub implementation of CloudProvider.

This class exists to demonstrate the extensibility of the provider
abstraction. The interface is fully defined — adding AWS support
requires implementing these methods with boto3.

To implement:
  pip install boto3
  Set env vars: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION

AWS equivalents of the Azure resources provisioned:
  Resource Group   → No direct equivalent (use tags or CloudFormation stack)
  Virtual Network  → VPC
  Subnet           → Subnet (within VPC)
  Public IP        → Elastic IP
  NSG              → Security Group
  NIC              → Elastic Network Interface (ENI)
  Virtual Machine  → EC2 Instance (t2.micro for free tier)
"""

from .base import CloudProvider, ProvisionConfig, VMStatus


class AWSProvider(CloudProvider):
    """
    AWS EC2 provider — currently a stub.
    Implement with boto3 to enable full AWS support.
    """

    def __init__(self):
        # boto3 would be initialised here:
        # import boto3
        # self._ec2 = boto3.client("ec2")
        pass

    def provision(self, config: ProvisionConfig, log=print) -> dict:
        """
        AWS equivalent: create VPC, subnet, security group, EC2 instance.

        boto3 rough sketch:
            ec2.create_vpc(CidrBlock="10.0.0.0/16")
            ec2.create_subnet(...)
            ec2.create_security_group(...)
            ec2.run_instances(ImageId="ami-...", InstanceType="t2.micro", ...)
        """
        raise NotImplementedError(
            "AWS provider is not yet implemented. "
            "To contribute, implement this method using boto3. "
            "See the docstring for the AWS equivalent resources."
        )

    def deploy(self, state: dict, log=print) -> None:
        """
        AWS equivalent: SSH into EC2 instance, install Docker, run container.
        The deploy logic is cloud-agnostic (pure SSH) so this would be
        nearly identical to AzureProvider.deploy().
        """
        raise NotImplementedError("AWS provider is not yet implemented.")

    def destroy(self, state: dict, log=print) -> None:
        """
        AWS equivalent: terminate EC2 instance, release Elastic IP,
        delete security group, delete subnet, delete VPC.

        boto3 rough sketch:
            ec2.terminate_instances(InstanceIds=[instance_id])
            ec2.release_address(AllocationId=eip_allocation_id)
            ec2.delete_security_group(GroupId=sg_id)
            ec2.delete_subnet(SubnetId=subnet_id)
            ec2.delete_vpc(VpcId=vpc_id)
        """
        raise NotImplementedError("AWS provider is not yet implemented.")

    def get_status(self, state: dict) -> VMStatus:
        """
        AWS equivalent: ec2.describe_instance_status(InstanceIds=[...])
        Maps EC2 states (pending/running/stopping/stopped/terminated)
        to the normalised VMStatus.state field.
        """
        raise NotImplementedError("AWS provider is not yet implemented.")
