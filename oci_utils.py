"""
Shared utilities for Oracle Cloud Infrastructure instance provisioning.
Contains common functions used by both polling and single-attempt modes.
"""

import configparser
import itertools
import json
import logging
import os
import smtplib
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Union

import oci
import paramiko
from dotenv import load_dotenv
import requests

# Constants
ARM_SHAPE = "VM.Standard.A1.Flex"
E2_MICRO_SHAPE = "VM.Standard.E2.1.Micro"

IMAGE_LIST_KEYS = [
    "lifecycle_state",
    "display_name", 
    "id",
    "operating_system",
    "operating_system_version",
    "size_in_mbs",
    "time_created",
]

class OCIConfig:
    """Configuration loader for OCI settings"""
    
    def __init__(self, env_file='oci.env'):
        # Load environment variables from .env file
        load_dotenv(env_file)
        
        # Access loaded environment variables and strip white spaces
        self.OCI_CONFIG = os.getenv("OCI_CONFIG", "").strip()
        self.OCT_FREE_AD = os.getenv("OCT_FREE_AD", "").strip()
        self.DISPLAY_NAME = os.getenv("DISPLAY_NAME", "").strip()
        self.WAIT_TIME = int(os.getenv("REQUEST_WAIT_TIME_SECS", "0").strip())
        self.SSH_AUTHORIZED_KEYS_FILE = os.getenv("SSH_AUTHORIZED_KEYS_FILE", "").strip()
        self.OCI_IMAGE_ID = os.getenv("OCI_IMAGE_ID", None).strip() if os.getenv("OCI_IMAGE_ID") else None
        self.OCI_COMPUTE_SHAPE = os.getenv("OCI_COMPUTE_SHAPE", ARM_SHAPE).strip()
        self.SECOND_MICRO_INSTANCE = os.getenv("SECOND_MICRO_INSTANCE", 'False').strip().lower() == 'true'
        self.OCI_SUBNET_ID = os.getenv("OCI_SUBNET_ID", None).strip() if os.getenv("OCI_SUBNET_ID") else None
        self.OPERATING_SYSTEM = os.getenv("OPERATING_SYSTEM", "").strip()
        self.OS_VERSION = os.getenv("OS_VERSION", "").strip()
        self.ASSIGN_PUBLIC_IP = os.getenv("ASSIGN_PUBLIC_IP", "false").strip()
        self.BOOT_VOLUME_SIZE = os.getenv("BOOT_VOLUME_SIZE", "50").strip()
        self.NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", 'False').strip().lower() == 'true'
        self.EMAIL = os.getenv("EMAIL", "").strip()
        self.EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "").strip()
        self.DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()
        self.MODE = os.getenv("MODE", "POLLING").strip().upper()
        
        # Validate configuration
        self._validate_config()
        
    def _validate_config(self):
        """Validate configuration settings"""
        # Read the configuration from oci_config file
        config = configparser.ConfigParser()
        try:
            config.read(self.OCI_CONFIG)
            self.OCI_USER_ID = config.get('DEFAULT', 'user')
            
            if self.OCI_COMPUTE_SHAPE not in (ARM_SHAPE, E2_MICRO_SHAPE):
                raise ValueError(f"{self.OCI_COMPUTE_SHAPE} is not an acceptable shape")
                
            env_has_spaces = any(isinstance(confg_var, str) and " " in confg_var
                                for confg_var in [self.OCI_CONFIG, self.OCT_FREE_AD, str(self.WAIT_TIME),
                                        self.SSH_AUTHORIZED_KEYS_FILE, self.OCI_IMAGE_ID, 
                                        self.OCI_COMPUTE_SHAPE, str(self.SECOND_MICRO_INSTANCE), 
                                        self.OCI_SUBNET_ID, self.OS_VERSION, str(self.NOTIFY_EMAIL), self.EMAIL,
                                        self.EMAIL_PASSWORD, self.DISCORD_WEBHOOK]
                                )
            config_has_spaces = any(' ' in value for section in config.sections() 
                                    for _, value in config.items(section))
            if env_has_spaces:
                raise ValueError("oci.env has spaces in values which is not acceptable")
            if config_has_spaces:
                raise ValueError("oci_config has spaces in values which is not acceptable")        

        except configparser.Error as e:
            with open("ERROR_IN_CONFIG.log", "w", encoding='utf-8') as file:
                file.write(str(e))
            raise e

def setup_oci_clients(config_obj: OCIConfig):
    """Set up OCI configuration and clients"""
    oci_config_path = config_obj.OCI_CONFIG if config_obj.OCI_CONFIG else "~/.oci/config"
    config = oci.config.from_file(oci_config_path)
    
    return {
        'config': config,
        'iam_client': oci.identity.IdentityClient(config),
        'network_client': oci.core.VirtualNetworkClient(config),
        'compute_client': oci.core.ComputeClient(config)
    }

def setup_logging(log_file="launch_instance.log"):
    """Set up logging configuration"""
    logging.basicConfig(
        filename="setup_and_info.log",
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    
    logger = logging.getLogger("launch_instance")
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(log_file)
    fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(fh)
    
    return logger

def handle_errors(command, data, log, wait_time):
    """
    Handles errors and logs messages.
    Returns True if error is retryable, False if fatal.
    """
    # Check for temporary errors that can be retried
    if "code" in data:
        if (data["code"] in ("TooManyRequests", "Out of host capacity.", 'InternalError')) \
                or (data["message"] in ("Out of host capacity.", "Bad Gateway")):
            log.info("Command: %s--\nOutput: %s", command, data)
            time.sleep(wait_time)
            return True

    if "status" in data and data["status"] == 502:
        log.info("Command: %s~~\nOutput: %s", command, data)
        time.sleep(wait_time)
        return True
        
    # Fatal error
    failure_msg = '\n'.join([f'{key}: {value}' for key, value in data.items()])
    notify_on_failure(failure_msg)
    return False

def execute_oci_command(client, method, *args, **kwargs):
    """Execute OCI command with error handling"""
    try:
        response = getattr(client, method)(*args, **kwargs)
        return response
    except oci.exceptions.ServiceError as srv_err:
        return {
            "code": srv_err.code,
            "message": srv_err.message,
            "status": srv_err.status
        }

def send_email(subject, body, email, password):
    """Send email notification"""
    try:
        msg = MIMEMultipart()
        msg['From'] = email
        msg['To'] = email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'html'))
        
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(email, password)
        text = msg.as_string()
        server.sendmail(email, email, text)
        server.quit()
        print("Email sent successfully")
    except Exception as e:
        print(f"Failed to send email: {e}")

def send_discord_message(webhook_url, message):
    """Send Discord webhook message"""
    if not webhook_url:
        return
        
    try:
        payload = {"content": message}
        response = requests.post(webhook_url, json=payload)
        if response.status_code == 204:
            print("Discord message sent successfully")
        else:
            print(f"Failed to send Discord message: {response.status_code}")
    except Exception as e:
        print(f"Failed to send Discord message: {e}")

def notify_on_failure(failure_msg):
    """Notify about failure via configured channels"""
    config_obj = OCIConfig()
    
    subject = "OCI Instance Creation Failed"
    
    if config_obj.NOTIFY_EMAIL:
        send_email(subject, failure_msg, config_obj.EMAIL, config_obj.EMAIL_PASSWORD)
    
    if config_obj.DISCORD_WEBHOOK:
        send_discord_message(config_obj.DISCORD_WEBHOOK, f"{subject}\n{failure_msg}")

def read_or_generate_ssh_public_key(public_key_file):
    """Read or generate SSH public key"""
    private_key_file = public_key_file.replace(".pub", "")
    
    if Path(public_key_file).is_file():
        with open(public_key_file, 'r', encoding='utf-8') as key_file:
            return key_file.read().strip()
    else:
        print(f"SSH public key file {public_key_file} not found. Generating a new key pair...")
        key = paramiko.RSAKey.generate(2048)
        
        # Save private key
        key.write_private_key_file(private_key_file)
        print(f"Private key saved to {private_key_file}")
        
        # Save public key
        public_key = f"{key.get_name()} {key.get_base64()}"
        with open(public_key_file, 'w', encoding='utf-8') as key_file:
            key_file.write(public_key)
        print(f"Public key saved to {public_key_file}")
        
        return public_key

def list_all_instances(compute_client, compartment_id):
    """List all instances in compartment"""
    return oci.pagination.list_call_get_all_results(
        compute_client.list_instances, compartment_id
    ).data

def check_instance_exists(compute_client, compartment_id, shape):
    """Check if instance with given shape already exists"""
    instances = list_all_instances(compute_client, compartment_id)
    
    for instance in instances:
        if (instance.shape == shape and 
            instance.lifecycle_state in ("RUNNING", "PROVISIONING", "STARTING")):
            return True, instance
    
    return False, None