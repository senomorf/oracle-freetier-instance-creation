#!/usr/bin/env python3
"""
Single-attempt OCI instance provisioning script.
Makes one attempt to create an instance and exits with appropriate status codes.

Exit codes:
- 0: Success (instance created or already exists)
- 1: Capacity issue (temporary, can retry later) 
- 2: Fatal error (configuration, permissions, etc.)
"""

import sys
import json
import itertools
from pathlib import Path

import oci
from oci_utils import (
    OCIConfig, setup_oci_clients, setup_logging, handle_errors,
    read_or_generate_ssh_public_key, check_instance_exists, notify_on_failure
)

def get_instance_creation_params(config_obj, clients):
    """Prepare all parameters needed for instance creation"""
    try:
        # Get tenancy (compartment)
        oci_tenancy = clients['config']['tenancy']
        
        # Get availability domain
        availability_domains = clients['iam_client'].list_availability_domains(oci_tenancy).data
        ad_names = [ad.name for ad in availability_domains]
        
        if config_obj.OCT_FREE_AD:
            if config_obj.OCT_FREE_AD not in ad_names:
                raise ValueError(f"Availability domain {config_obj.OCT_FREE_AD} not found")
            oci_ad_names = itertools.cycle([config_obj.OCT_FREE_AD])
        else:
            oci_ad_names = itertools.cycle(ad_names)
        
        # Get subnet
        if config_obj.OCI_SUBNET_ID:
            oci_subnet_id = config_obj.OCI_SUBNET_ID
        else:
            vcns = clients['network_client'].list_vcns(oci_tenancy).data
            if not vcns:
                raise ValueError("No VCNs found")
            
            subnets = clients['network_client'].list_subnets(oci_tenancy, vcn_id=vcns[0].id).data
            public_subnets = [s for s in subnets if not s.prohibit_public_ip_on_vnic]
            if not public_subnets:
                raise ValueError("No public subnets found")
            oci_subnet_id = public_subnets[0].id
        
        # Get image
        if config_obj.OCI_IMAGE_ID:
            oci_image_id = config_obj.OCI_IMAGE_ID
        else:
            images = clients['compute_client'].list_images(
                oci_tenancy,
                operating_system=config_obj.OPERATING_SYSTEM,
                operating_system_version=config_obj.OS_VERSION
            ).data
            if not images:
                raise ValueError(f"No images found for {config_obj.OPERATING_SYSTEM} {config_obj.OS_VERSION}")
            oci_image_id = images[0].id
        
        # Shape configuration
        if config_obj.OCI_COMPUTE_SHAPE.startswith("VM.Standard.A1"):
            shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=1,
                memory_in_gbs=6,
                baseline_ocpu_utilization="BASELINE_1_1"
            )
        else:
            shape_config = None
        
        # SSH key
        ssh_public_key = read_or_generate_ssh_public_key(config_obj.SSH_AUTHORIZED_KEYS_FILE)
        
        # Other settings
        assign_public_ip = config_obj.ASSIGN_PUBLIC_IP.lower() == "true"
        boot_volume_size = int(config_obj.BOOT_VOLUME_SIZE)
        
        return {
            'tenancy': oci_tenancy,
            'ad_names': oci_ad_names,
            'subnet_id': oci_subnet_id,
            'image_id': oci_image_id,
            'shape_config': shape_config,
            'ssh_public_key': ssh_public_key,
            'assign_public_ip': assign_public_ip,
            'boot_volume_size': boot_volume_size
        }
        
    except Exception as e:
        print(f"Error preparing instance parameters: {e}")
        return None

def attempt_instance_creation(config_obj, clients, params, logger):
    """
    Make a single attempt to create an instance.
    Returns (success: bool, retry_possible: bool, message: str)
    """
    try:
        # Check if instance already exists
        exists, existing_instance = check_instance_exists(
            clients['compute_client'], 
            params['tenancy'], 
            config_obj.OCI_COMPUTE_SHAPE
        )
        
        if exists:
            logger.info(f"Instance already exists: {existing_instance.display_name}")
            return True, False, f"Instance already exists: {existing_instance.display_name}"
        
        # Attempt to create instance
        launch_instance_response = clients['compute_client'].launch_instance(
            launch_instance_details=oci.core.models.LaunchInstanceDetails(
                availability_domain=next(params['ad_names']),
                compartment_id=params['tenancy'],
                create_vnic_details=oci.core.models.CreateVnicDetails(
                    assign_public_ip=params['assign_public_ip'],
                    assign_private_dns_record=True,
                    display_name=config_obj.DISPLAY_NAME,
                    subnet_id=params['subnet_id'],
                ),
                display_name=config_obj.DISPLAY_NAME,
                shape=config_obj.OCI_COMPUTE_SHAPE,
                availability_config=oci.core.models.LaunchInstanceAvailabilityConfigDetails(
                    recovery_action="RESTORE_INSTANCE"
                ),
                instance_options=oci.core.models.InstanceOptions(
                    are_legacy_imds_endpoints_disabled=False
                ),
                shape_config=params['shape_config'],
                source_details=oci.core.models.InstanceSourceViaImageDetails(
                    source_type="image",
                    image_id=params['image_id'],
                    boot_volume_size_in_gbs=params['boot_volume_size'],
                ),
                metadata={
                    "ssh_authorized_keys": params['ssh_public_key']
                },
            )
        )
        
        if launch_instance_response.status == 200:
            logger.info(f"Instance creation initiated: {launch_instance_response.data.id}")
            
            # Create success marker file
            with open("INSTANCE_CREATED", "w", encoding='utf-8') as f:
                f.write("SUCCESS")
            
            return True, False, f"Instance creation successful: {launch_instance_response.data.id}"
        else:
            return False, False, f"Unexpected response status: {launch_instance_response.status}"
            
    except oci.exceptions.ServiceError as srv_err:
        # Handle specific service errors
        if srv_err.code == "LimitExceeded":
            logger.info(f"LimitExceeded error: {srv_err.message}")
            # Check if instance was actually created despite the error
            exists, existing_instance = check_instance_exists(
                clients['compute_client'], 
                params['tenancy'], 
                config_obj.OCI_COMPUTE_SHAPE
            )
            if exists:
                logger.info(f"Instance was created despite LimitExceeded error: {existing_instance.display_name}")
                with open("INSTANCE_CREATED", "w", encoding='utf-8') as f:
                    f.write("SUCCESS")
                return True, False, f"Instance created: {existing_instance.display_name}"
        
        # Check if this is a retryable error
        error_data = {
            "status": srv_err.status,
            "code": srv_err.code,
            "message": srv_err.message,
        }
        
        # Check for capacity-related errors (retryable)
        if (srv_err.code in ("TooManyRequests", "Out of host capacity.", "InternalError") or
            srv_err.message in ("Out of host capacity.", "Bad Gateway") or
            srv_err.status == 502):
            logger.info(f"Capacity issue: {srv_err.code} - {srv_err.message}")
            return False, True, f"Capacity issue: {srv_err.message}"
        
        # Fatal error
        logger.error(f"Fatal error: {srv_err.code} - {srv_err.message}")
        return False, False, f"Fatal error: {srv_err.message}"
        
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return False, False, f"Unexpected error: {e}"

def main():
    """Main function for single-attempt provisioning"""
    try:
        # Load configuration
        config_obj = OCIConfig()
        
        # Set up logging
        logger = setup_logging("provision_once.log")
        logger.info("Starting single-attempt instance provisioning")
        
        # Set up OCI clients
        clients = setup_oci_clients(config_obj)
        
        # Get instance creation parameters
        params = get_instance_creation_params(config_obj, clients)
        if not params:
            logger.error("Failed to prepare instance parameters")
            print("Failed to prepare instance parameters")
            sys.exit(2)
        
        # Attempt instance creation
        success, retry_possible, message = attempt_instance_creation(
            config_obj, clients, params, logger
        )
        
        # Log and print result
        logger.info(f"Result: success={success}, retry_possible={retry_possible}, message={message}")
        print(message)
        
        # Exit with appropriate code
        if success:
            sys.exit(0)  # Success
        elif retry_possible:
            sys.exit(1)  # Capacity issue - can retry later
        else:
            sys.exit(2)  # Fatal error
            
    except Exception as e:
        print(f"Critical error: {e}")
        if 'logger' in locals():
            logger.error(f"Critical error: {e}")
        sys.exit(2)

if __name__ == "__main__":
    main()