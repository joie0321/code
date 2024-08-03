from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
from pyVim.task import WaitForTasks
from Crypto.Cipher import AES
import ssl
import OpenSSL.crypto
import logging
import json
import argparse
import time
from typing import Optional, Dict, Union
import getpass
import oci
import base64

def user_input():
    vcenter_ip = input("Enter your OCVS VC IP: ")
    vc_password = getpass.getpass("Enter your OCVS VC password: ")
    esxi_password = getpass.getpass("Enter your ESXi VC password: ")
    datacenter_name = input("Enter your OCVS Datacenter Name: ")
    cluster_name = input("Enter your Workload Cluster Name: ")
    
    return vcenter_ip, vc_password, esxi_password, datacenter_name, cluster_name

def get_args():
    parser = argparse.ArgumentParser(description='Process some JSON file.')
    parser.add_argument('file_name', type=str, help='The JSON file to read')
    args = parser.parse_args()
    
    return args
    
def read_json_file(file_name):
    try:
        with open(file_name, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"{file_name} not found.")
        return None
    except json.JSONDecodeError:
        print(f"Invalid JSON in {file_name}.")
        return None

def process_ocvs_info(data):
    esxi_hosts = []
    subnet_info = data.get("Subnet Information", {})
    for subnet_name, details in subnet_info.items():
        for detail in details:
            subnet_domain_fqdn = detail.get("Subnet Domain FQDN")
            esxi_hosts.append(subnet_domain_fqdn)
    
    vlan_details = []
    sddc_vlan_details = data.get("SDDC VLAN Details", [])
    for vlan_detail in sddc_vlan_details:
        vlan_name = vlan_detail.get("VLAN Name")
        vlan_tag = vlan_detail.get("VLAN Tag")
        vlan_cidr = vlan_detail.get("VLAN CIDR Block")
        vlan_details.append({"VLAN Name": vlan_name, "VLAN Tag": vlan_tag, "VLAN CIDR Block": vlan_cidr})
    
    # Print or further process the results stored in arrays
    print("Subnet Domain FQDNs:")
    for fqdn in esxi_hosts:
        print(f"  {fqdn}")
    
    print("SDDC VLAN Details:")
    for vlan in vlan_details:
        print(f"  VLAN Name: {vlan['VLAN Name']}, VLAN Tag: {vlan['VLAN Tag']}, VLAN CIDR Block: {vlan['VLAN CIDR Block']}")

    return esxi_hosts, vlan_details
    
def _init_logging():
    # Get the root logger
    logger = logging.getLogger()

    # Set the logging level to INFO
    logger.setLevel(logging.INFO)

    # Create a formatter that includes the date and time
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    # Add a console handler and set its formatter to the created formatter
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    logging.info("Starting the script to add the host to the cluster...")

def wait_for_vc_tasks_to_clear(si):
    logging.info("Waiting for existing vCenter tasks to complete")

    task_manager = si.content.taskManager
    task_filter_spec = vim.TaskFilterSpec()
    task_filter_spec.state = vim.TaskInfo.State.running
    task_collector = task_manager.CreateCollectorForTasks(task_filter_spec)

    # Wait for the collector to complete the search
    tasks = task_collector.ReadNextTasks(maxCount=100)
    running_tasks= []
    if tasks:
        for task in tasks:
            running_tasks.append(task.task)
            logging.info(f"Waiting for the {task.descriptionId} to clear")
        #Wait for any running tasks to complete
        try:
            WaitForTasks(running_tasks,si=si)
        except Exception as e:
            logging.error(f"Caught exception: {e.msg}")

    # Close the collector to release resources
    task_collector.ResetCollector()

def connect_to_vcenter(vcenter_ip, vc_username, vc_password, vc_context):
    logging.info("Connecting to VC.....")
    try:
        si = SmartConnect(host=vcenter_ip, user=vc_username, pwd=vc_password, sslContext=vc_context)
        vc = si.RetrieveContent()
        return vc, si
    except vim.fault.InvalidLogin as e:
        logging.error("Invalid login credentials: %s", e)
        raise
    except Exception as e:
        logging.error("Failed to connect to vCenter: %s", e)
        raise

def get_datacenter(vc, datacenter_name):
    """
    Get a reference to the target datacenter managed by the vCenter.
    """
    logging.info("Getting datacenter object......")
    try:
        # Create a container view for all datacenters
        containerView = vc.viewManager.CreateContainerView(vc.rootFolder, [vim.Datacenter], True)

        # Iterate through the container view to find the datacenter by name
        for datacenter in containerView.view:
            if datacenter.name == datacenter_name:
                return datacenter

        # If datacenter is not found
        logging.error(f"Datacenter '{datacenter_name}' not found.")
        return None

    except Exception as e:
        logging.error("Failed to retrieve the datacenter information: %s", e)
        raise
    finally:
        # Destroy the container view to release resources
        if containerView:
            containerView.Destroy()


def get_esxi_host_thumbprint(esxi_host, esxi_username, esxi_password, vc_context):
    """
    Connect to an ESXi host and retrieve the SSL thumbprint.
    """
    logging.info(f"Getting ESXi  {esxi_host}  thumbprints")
    try:
        service_instance = SmartConnect(host=esxi_host, user=esxi_username, pwd=esxi_password, port=443, sslContext=vc_context)
        cert = ssl.get_server_certificate((esxi_host, 443))
        x509 = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM, cert)
        thumbprint = x509.digest("sha1").decode("utf-8")
        Disconnect(service_instance)
        return thumbprint
    except Exception as e:
        logging.error("Failed to retrieve the ESXi host thumbprint: %s", e)
        raise

def create_connect_spec(esxi_host, esxi_username, esxi_password, thumbprint):
    """
    Create a ConnectSpec instance to add the ESXi host to the cluster.
    """
    logging.info("Creating connection specification to add ESXi host to the cluster")
    spec = vim.host.ConnectSpec()
    spec.hostName = esxi_host
    spec.userName = esxi_username
    spec.password = esxi_password
    spec.sslThumbprint = thumbprint
    spec.force = True
    logging.debug(f"Connection specification to add the ESXi host to the cluster  {spec} ")
    return spec

def add_esxi_hosts_to_cluster(cluster, spec):
    """
    Add the ESXi host to the cluster.
    """
    logging.info("Adding ESXi host to cluster")
    try:
        task = cluster.AddHost(spec, True, None)
        return task
    except vim.fault.AlreadyBeingManagedByCluster:
        logging.error("ESXi host is already a member of the cluster")
        raise
    except Exception as e:
        logging.error("Failed to add ESXi host to the cluster: %s", e)
        raise

def create_dvs(datacenter, cluster_name):
    """
    Create a distributed virtual switch (DVS) with two uplinks and set MTU to 9000.
    """
    logging.info("Creating DVS...")
    try:
        dvs_display_name = f"{cluster_name}-DSwitch"  # Construct the DVS name
        dvs_spec = vim.DVSCreateSpec()
        dvs_spec.configSpec = vim.VMwareDVSConfigSpec(name=dvs_display_name)
        
        # Add two uplinks
        dvs_spec.configSpec.uplinkPortPolicy = vim.DVSNameArrayUplinkPortPolicy()
        dvs_spec.configSpec.uplinkPortPolicy.uplinkPortName = ["uplink-vmnic0", "uplink-vmnic1"]
        
        # Create the DVS
        task = datacenter.networkFolder.CreateDVS_Task(dvs_spec)
        WaitForTasks([task])  # Wait for the task to complete
        dvs = task.info.result  # Get the result of the task
        dvs_uuid = dvs.uuid
        logging.info("DVS created successfully: %s", dvs_display_name)
        
        # Set the MTU to 9000
        dvs_config_spec = vim.VMwareDVSConfigSpec()
        dvs_config_spec.configVersion = dvs.config.configVersion
        dvs_config_spec.maxMtu = 9000
        
        reconfig_task = dvs.ReconfigureDvs_Task(dvs_config_spec)
        WaitForTasks([reconfig_task])
        
        return dvs, dvs_display_name, dvs_uuid
    
    except Exception as e:
        logging.error("Failed to create DVS: %s", e)
        raise

def create_dvs_port_group_management(dvs, cluster_name):
    """
    Create a distributed port group on the specified DVS.
    """
    logging.info("Creating distributed port group...")
    try:
        port_group_name = f"{cluster_name}-Management"  # Construct the port group name
        pg_spec = vim.dvs.DistributedVirtualPortgroup.ConfigSpec()
        pg_spec.name = port_group_name
        pg_spec.type = vim.dvs.DistributedVirtualPortgroup.PortgroupType.ephemeral
        
        task = dvs.CreateDVPortgroup_Task(pg_spec)
        WaitForTasks([task])  # Wait for the task to complete
        port_group = task.info.result  # Get the result of the task
        logging.info("Port group created successfully: %s", port_group_name)
        return port_group
    
    except Exception as e:
        logging.error("Failed to create port group: %s", e)
        raise

def reconfigure_dvport_group_mgmt(dpg_management):
    """
    Reconfigure a distributed port group with specified properties.
    """
    logging.info("Reconfiguring distributed port group...")
    try:
        # Create a Distributed Port Group configuration specification
        dpg_spec = vim.dvs.DistributedVirtualPortgroup.ConfigSpec()
        
        # Update uplink teaming policy
        dpg_spec.defaultPortConfig = vim.dvs.VmwareDistributedVirtualSwitch.VmwarePortConfigPolicy()
        dpg_spec.defaultPortConfig.uplinkTeamingPolicy = vim.dvs.VmwareDistributedVirtualSwitch.UplinkPortTeamingPolicy()

        # Set inherited to false for all properties
        dpg_spec.defaultPortConfig.uplinkTeamingPolicy.inherited = False
        dpg_spec.defaultPortConfig.uplinkTeamingPolicy.policy = vim.StringPolicy()
        dpg_spec.defaultPortConfig.uplinkTeamingPolicy.policy.inherited = False
        dpg_spec.defaultPortConfig.uplinkTeamingPolicy.policy.value = "failover_explicit"
        
        dpg_spec.defaultPortConfig.uplinkTeamingPolicy.uplinkPortOrder = vim.dvs.VmwareDistributedVirtualSwitch.UplinkPortOrderPolicy()
        dpg_spec_defaultPortConfig_uplinkTeamingPolicy_uplinkPortOrder_activeUplinkPort_0 = 'uplink-vmnic0'
        dpg_spec.defaultPortConfig.uplinkTeamingPolicy.uplinkPortOrder.activeUplinkPort = [dpg_spec_defaultPortConfig_uplinkTeamingPolicy_uplinkPortOrder_activeUplinkPort_0]
        dpg_spec.defaultPortConfig.uplinkTeamingPolicy.uplinkPortOrder.inherited = False
        dpg_spec_defaultPortConfig_uplinkTeamingPolicy_uplinkPortOrder_standbyUplinkPort_0 = 'uplink-vmnic1'
        dpg_spec.defaultPortConfig.uplinkTeamingPolicy.uplinkPortOrder.standbyUplinkPort = [dpg_spec_defaultPortConfig_uplinkTeamingPolicy_uplinkPortOrder_standbyUplinkPort_0]

        # Set configVersion to current version of the Distributed Port Group
        dpg_spec.configVersion = dpg_management.config.configVersion
        
        # Reconfigure the Distributed Port Group
        task = dpg_management.ReconfigureDVPortgroup_Task(dpg_spec)
        WaitForTasks([task])  # Wait for the task to complete
        logging.info("Distributed Port Group reconfigured successfully.")
    except Exception as e:
        logging.error("Failed to reconfigure Distributed Port Group: %s", e)
        raise

def create_dvs_port_group_provisioning(dvs, cluster_name, vlan_tag):
    """
    Create a distributed port group on the specified DVS.
    """
    logging.info("Creating distributed port group...")
    try:
        port_group_name = f"{cluster_name}-Provisioning"  # Construct the port group name
        pg_spec = vim.dvs.DistributedVirtualPortgroup.ConfigSpec()
        pg_spec.name = port_group_name
        pg_spec.type = vim.dvs.DistributedVirtualPortgroup.PortgroupType.ephemeral
        pg_spec.defaultPortConfig = vim.dvs.VmwareDistributedVirtualSwitch.VmwarePortConfigPolicy()
        pg_spec.defaultPortConfig.vlan = vim.dvs.VmwareDistributedVirtualSwitch.VlanIdSpec()
        pg_spec.defaultPortConfig.vlan.vlanId = vlan_tag
        pg_spec.defaultPortConfig.uplinkTeamingPolicy = vim.dvs.VmwareDistributedVirtualSwitch.UplinkPortTeamingPolicy()
        pg_spec.defaultPortConfig.uplinkTeamingPolicy.inherited = False
        pg_spec.defaultPortConfig.uplinkTeamingPolicy.policy = vim.StringPolicy()
        pg_spec.defaultPortConfig.uplinkTeamingPolicy.policy.inherited = False
        pg_spec.defaultPortConfig.uplinkTeamingPolicy.policy.value = "loadbalance_loadbased"

        task = dvs.CreateDVPortgroup_Task(pg_spec)
        WaitForTasks([task])  # Wait for the task to complete
        port_group = task.info.result  # Get the result of the task
        logging.info("Port group created successfully: %s", port_group_name)
        return port_group
    except Exception as e:
        logging.error("Failed to create port group: %s", e)
        raise

def create_dvs_port_group_replication(dvs, cluster_name, vlan_tag):
    """
    Create a distributed port group on the specified DVS.
    """
    logging.info("Creating distributed port group...")
    try:
        port_group_name = f"{cluster_name}-Replication"  # Construct the port group name
        pg_spec = vim.dvs.DistributedVirtualPortgroup.ConfigSpec()
        pg_spec.name = port_group_name
        pg_spec.type = vim.dvs.DistributedVirtualPortgroup.PortgroupType.ephemeral
        pg_spec.defaultPortConfig = vim.dvs.VmwareDistributedVirtualSwitch.VmwarePortConfigPolicy()
        pg_spec.defaultPortConfig.vlan = vim.dvs.VmwareDistributedVirtualSwitch.VlanIdSpec()
        pg_spec.defaultPortConfig.vlan.vlanId = vlan_tag
        pg_spec.defaultPortConfig.uplinkTeamingPolicy = vim.dvs.VmwareDistributedVirtualSwitch.UplinkPortTeamingPolicy()
        pg_spec.defaultPortConfig.uplinkTeamingPolicy.inherited = False
        pg_spec.defaultPortConfig.uplinkTeamingPolicy.policy = vim.StringPolicy()
        pg_spec.defaultPortConfig.uplinkTeamingPolicy.policy.inherited = False
        pg_spec.defaultPortConfig.uplinkTeamingPolicy.policy.value = "loadbalance_loadbased"

        task = dvs.CreateDVPortgroup_Task(pg_spec)
        WaitForTasks([task])  # Wait for the task to complete
        port_group = task.info.result  # Get the result of the task
        logging.info("Port group created successfully: %s", port_group_name)
        return port_group
    except Exception as e:
        logging.error("Failed to create port group: %s", e)
        raise

def create_dvs_port_group_vmotion(dvs, cluster_name, vlan_tag):
    """
    Create a distributed port group on the specified DVS.
    """
    logging.info("Creating distributed port group...")
    try:
        port_group_name = f"{cluster_name}-vMotion"  # Construct the port group name
        pg_spec = vim.dvs.DistributedVirtualPortgroup.ConfigSpec()
        pg_spec.name = port_group_name
        pg_spec.type = vim.dvs.DistributedVirtualPortgroup.PortgroupType.ephemeral
        pg_spec.defaultPortConfig = vim.dvs.VmwareDistributedVirtualSwitch.VmwarePortConfigPolicy()
        pg_spec.defaultPortConfig.vlan = vim.dvs.VmwareDistributedVirtualSwitch.VlanIdSpec()
        pg_spec.defaultPortConfig.vlan.vlanId = vlan_tag
        pg_spec.defaultPortConfig.uplinkTeamingPolicy = vim.dvs.VmwareDistributedVirtualSwitch.UplinkPortTeamingPolicy()
        pg_spec.defaultPortConfig.uplinkTeamingPolicy.inherited = False
        pg_spec.defaultPortConfig.uplinkTeamingPolicy.policy = vim.StringPolicy()
        pg_spec.defaultPortConfig.uplinkTeamingPolicy.policy.inherited = False
        pg_spec.defaultPortConfig.uplinkTeamingPolicy.policy.value = "loadbalance_loadbased"

        task = dvs.CreateDVPortgroup_Task(pg_spec)
        WaitForTasks([task])  # Wait for the task to complete
        port_group = task.info.result  # Get the result of the task
        logging.info("Port group created successfully: %s", port_group_name)
        return port_group
    except Exception as e:
        logging.error("Failed to create port group: %s", e)
        raise

def create_dvs_port_group_vsan(dvs, cluster_name, vlan_tag):
    """
    Create a distributed port group on the specified DVS.
    """
    logging.info("Creating distributed port group...")
    try:
        port_group_name = f"{cluster_name}-vSAN"  # Construct the port group name
        pg_spec = vim.dvs.DistributedVirtualPortgroup.ConfigSpec()
        pg_spec.name = port_group_name
        pg_spec.type = vim.dvs.DistributedVirtualPortgroup.PortgroupType.ephemeral
        pg_spec.defaultPortConfig = vim.dvs.VmwareDistributedVirtualSwitch.VmwarePortConfigPolicy()
        pg_spec.defaultPortConfig.vlan = vim.dvs.VmwareDistributedVirtualSwitch.VlanIdSpec()
        pg_spec.defaultPortConfig.vlan.vlanId = vlan_tag
        pg_spec.defaultPortConfig.uplinkTeamingPolicy = vim.dvs.VmwareDistributedVirtualSwitch.UplinkPortTeamingPolicy()
        pg_spec.defaultPortConfig.uplinkTeamingPolicy.inherited = False
        pg_spec.defaultPortConfig.uplinkTeamingPolicy.policy = vim.StringPolicy()
        pg_spec.defaultPortConfig.uplinkTeamingPolicy.policy.inherited = False
        pg_spec.defaultPortConfig.uplinkTeamingPolicy.policy.value = "loadbalance_loadbased"

        task = dvs.CreateDVPortgroup_Task(pg_spec)
        WaitForTasks([task])  # Wait for the task to complete
        port_group = task.info.result  # Get the result of the task
        logging.info("Port group created successfully: %s", port_group_name)
        return port_group
    except Exception as e:
        logging.error("Failed to create port group: %s", e)
        raise
    
def list_esxi_hosts_in_cluster(cluster):
    """
    List all ESXi hosts in the specified cluster.
    """
    logging.info("Listing ESXi hosts in the cluster...")
    try:
        # Retrieve the hosts from the cluster
        esxi_host_objects = cluster.host
        esxi_hosts_list = []

        # Iterate through the hosts and collect their names
        for esxi_host_object in esxi_host_objects:
            if isinstance(esxi_host_object, vim.HostSystem):
                esxi_hosts_list.append(esxi_host_object.name)
                logging.info(f"ESXi Host: {esxi_host_object.name}")
        
        return esxi_hosts_list, esxi_host_objects

    except Exception as e:
        logging.error(f"Failed to list ESXi hosts in the cluster: {e}")
        raise

def host_maintenance_mode(esxi_host_object):
    tasks = []
    """
    Enables maintenance mode on each ESXi host in the list.
    """
    try:
    # Enter the maintenance mode
        logging.info("Enabling maintenance mode for host %s", esxi_host_object.name)
        task = esxi_host_object.EnterMaintenanceMode(timeout=0)
        tasks.append(task)
    except vim.fault.InvalidState:
        # If the host is already in maintenance mode, log a message and continue
        logging.warning("Host %s is already in maintenance mode", esxi_host_object.name)
    except vim.fault.TaskInProgress:
        # If a task is already in progress, log a message and continue
        logging.warning("A task is already in progress for host %s", esxi_host_object.name)
    except Exception as e:
        # Log any other exceptions that occur
        logging.error("Error enabling maintenance mode for host %s: %s", esxi_host_object.name, str(e))


def retrieve_specific_host_and_dvs (si, datacenter, cluster, dvs_name, esxi_host_name):
    content = si.RetrieveContent()
        
    # Get the specific cluster's host list
    hosts = cluster.host
        
    # Get the specific datacenter's network folder
    network_folder = datacenter.networkFolder

    # Search for the DVS within the datacenter's network folder
    dvs = None
    for network in network_folder.childEntity:
        if isinstance(network, vim.DistributedVirtualSwitch) and network.name == dvs_name.name:
            dvs = network
            break
        
    if not dvs:
        logging.error(f"Could not find Distributed Virtual Switch with name {dvs_name.name}.")
        return None, None

    # Retrieve the vim.HostSystem object for the specified ESXi host name within the cluster
    esxi_host = None
    for host in hosts:
        if host.name == esxi_host_name:
            esxi_host = host
            break

    if not esxi_host:
        logging.error(f"Could not find ESXi host with name {esxi_host_name} in cluster {cluster.name}.")
        logging.info(f"Available hosts in cluster {cluster.name}: {[host.name for host in hosts]}")
        return None, None
    
    return esxi_host, dvs

def get_vswitch_ports(esxi_host_object):
    network_system = esxi_host_object.configManager.networkSystem
    vswitches = network_system.networkInfo.vswitch

    vmkernel_list = []
    for vswitch in vswitches:
        if vswitch.name == "vSwitch0":
            vmkernel_ports = esxi_host_object.config.network.vnic
            for vnic in vmkernel_ports:
                if vnic.spec.distributedVirtualPort is None:
                    port_info = vnic.portgroup
                    vmkernel_list.append(port_info)
    return vmkernel_list

def reconfigureDvs(dvs_name, esxi_host_name, si, datacenter, cluster):
    logging.info("Reconfigure distributed switch add the host started....")
    
    try:
        esxi_host, dvs = retrieve_specific_host_and_dvs(si, datacenter, cluster, dvs_name, esxi_host_name)
        
        if esxi_host and dvs:
            spec = vim.DistributedVirtualSwitch.ConfigSpec()
            spec.configVersion = dvs_name.config.configVersion
            spec_host_0 = vim.dvs.HostMember.ConfigSpec()
            spec_host_0.host = esxi_host
            spec_host_0.operation = 'add'
            spec.host = [spec_host_0]
            dvs.ReconfigureDvs_Task(spec)
            logging.info(f"Successfully reconfigured the DVS {dvs_name.name} with host {esxi_host_name}.")
    except Exception as e:
        logging.error(f"Failed to reconfigure the DVS {dvs_name.name} with host {esxi_host_name}. Error: {e}")

def migrate_host_switch(datacenter, esxi_host_object,dvs_name, dpg_management, dpg_provisioning, dpg_replication, dpg_vmotion, dpg_vsan):
    
    network_system = esxi_host_object.configManager.networkSystem
    
    # Get the specific datacenter's network folder
    network_folder = datacenter.networkFolder

    # Retrieve the distributed virtual switch (VDS) by name
    dvs = None
    for dvs_obj in network_folder.childEntity:
        if isinstance(dvs_obj, vim.DistributedVirtualSwitch) and dvs_obj.name == dvs_name.name:
            dvs = dvs_obj
            break

    if not dvs:
        raise RuntimeError(f"Failed to find distributed virtual switch with name '{dvs_name}'")
    
    config = vim.host.NetworkConfig()
    
    #Create config spec for vSphere standard switch
    config_vswitch_0 = vim.host.VirtualSwitch.Config()
    config_vswitch_0.name = 'vSwitch0'
    config_vswitch_0.changeOperation = 'edit'
    config_vswitch_0.spec = vim.host.VirtualSwitch.Specification()
    config_vswitch_0.spec.numPorts = 128
    config_vswitch_0.spec.policy = vim.host.NetworkPolicy()
    config_vswitch_0.spec.policy.security = vim.host.NetworkPolicy.SecurityPolicy()
    config_vswitch_0.spec.policy.security.allowPromiscuous = False
    config_vswitch_0.spec.policy.security.forgedTransmits = False
    config_vswitch_0.spec.policy.security.macChanges = False
    config_vswitch_0.spec.policy.offloadPolicy = vim.host.NetOffloadCapabilities()
    config_vswitch_0.spec.policy.offloadPolicy.tcpSegmentation = True
    config_vswitch_0.spec.policy.offloadPolicy.zeroCopyXmit = True
    config_vswitch_0.spec.policy.offloadPolicy.csumOffload = True
    config_vswitch_0.spec.policy.shapingPolicy = vim.host.NetworkPolicy.TrafficShapingPolicy()
    config_vswitch_0.spec.policy.shapingPolicy.enabled = False
    config_vswitch_0.spec.policy.nicTeaming = vim.host.NetworkPolicy.NicTeamingPolicy()
    config_vswitch_0.spec.policy.nicTeaming.notifySwitches = True
    config_vswitch_0.spec.policy.nicTeaming.rollingOrder = False
    config_vswitch_0.spec.policy.nicTeaming.failureCriteria = vim.host.NetworkPolicy.NicFailureCriteria()
    config_vswitch_0.spec.policy.nicTeaming.failureCriteria.fullDuplex = False
    config_vswitch_0.spec.policy.nicTeaming.failureCriteria.percentage = 0
    config_vswitch_0.spec.policy.nicTeaming.failureCriteria.checkErrorPercent = False
    config_vswitch_0.spec.policy.nicTeaming.failureCriteria.checkDuplex = False
    config_vswitch_0.spec.policy.nicTeaming.failureCriteria.checkBeacon = False
    config_vswitch_0.spec.policy.nicTeaming.failureCriteria.speed = 10
    config_vswitch_0.spec.policy.nicTeaming.failureCriteria.checkSpeed = 'minimum'
    config_vswitch_0.spec.policy.nicTeaming.policy = 'loadbalance_srcid'
    config_vswitch_0.spec.policy.nicTeaming.reversePolicy = True
    config.vswitch = [config_vswitch_0]

    # Config Spec to remove the standard switch port groups after migration.
    logging.info(f"Getting {esxi_host_object} vSwitch information")
    vmkernel_list = get_vswitch_ports(esxi_host_object)
    
    portgroup_configs = []
    for portgroup_name in vmkernel_list:
        print("Reconfiguring",portgroup_name)
        config_portgroup = vim.host.PortGroup.Config()
        config_portgroup.changeOperation = 'remove'
        config_portgroup.spec = vim.host.PortGroup.Specification()
        config_portgroup.spec.vswitchName = 'vSwitch0'
        config_portgroup.spec.name = portgroup_name
        config_portgroup.spec.policy = vim.host.NetworkPolicy()
        portgroup_configs.append(config_portgroup)
        
    config.portgroup = portgroup_configs
        
    # Fetch port group information
    portgroups = dvs.portgroup

    portgroup_info = {}
    for pg in portgroups:
        portgroup_info[pg.key] = pg.name

    # Initialize config_vnic variables
    config_vnic_0 = None
    config_vnic_1 = None
    config_vnic_2 = None
    config_vnic_3 = None
    config_vnic_4 = None
    config_proxySwitch_0 = None

    # Assign port group keys to config_vnic_* objects
    for pg in portgroups:
        pg_name = portgroup_info[pg.key]

        # vmk0
        if pg_name == dpg_management.name:
            config_vnic_0 = vim.host.VirtualNic.Config()
            config_vnic_0.portgroup = ''
            config_vnic_0.device = 'vmk0'
            config_vnic_0.changeOperation = 'edit'
            config_vnic_0.spec = vim.host.VirtualNic.Specification()
            config_vnic_0.spec.distributedVirtualPort = vim.dvs.PortConnection()
            config_vnic_0.spec.distributedVirtualPort.switchUuid = dvs.uuid
            config_vnic_0.spec.distributedVirtualPort.portgroupKey = pg.key

        # vmk1
        if pg_name == dpg_vmotion.name:
            config_vnic_1 = vim.host.VirtualNic.Config()
            config_vnic_1.portgroup = ''
            config_vnic_1.device = 'vmk1'
            config_vnic_1.changeOperation = 'edit'
            config_vnic_1.spec = vim.host.VirtualNic.Specification()
            config_vnic_1.spec.distributedVirtualPort = vim.dvs.PortConnection()
            config_vnic_1.spec.distributedVirtualPort.switchUuid = dvs.uuid
            config_vnic_1.spec.distributedVirtualPort.portgroupKey = pg.key

        # vmk2
        if pg_name == dpg_vsan.name:
            config_vnic_2 = vim.host.VirtualNic.Config()
            config_vnic_2.portgroup = ''
            config_vnic_2.device = 'vmk2'
            config_vnic_2.changeOperation = 'edit'
            config_vnic_2.spec = vim.host.VirtualNic.Specification()
            config_vnic_2.spec.distributedVirtualPort = vim.dvs.PortConnection()
            config_vnic_2.spec.distributedVirtualPort.switchUuid = dvs.uuid
            config_vnic_2.spec.distributedVirtualPort.portgroupKey = pg.key

        # vmk3
        if pg_name == dpg_replication.name:
            config_vnic_3 = vim.host.VirtualNic.Config()
            config_vnic_3.portgroup = ''
            config_vnic_3.device = 'vmk3'
            config_vnic_3.changeOperation = 'edit'
            config_vnic_3.spec = vim.host.VirtualNic.Specification()
            config_vnic_3.spec.distributedVirtualPort = vim.dvs.PortConnection()
            config_vnic_3.spec.distributedVirtualPort.switchUuid = dvs.uuid
            config_vnic_3.spec.distributedVirtualPort.portgroupKey = pg.key

        # vmk4
        if pg_name == dpg_provisioning.name:
            config_vnic_4 = vim.host.VirtualNic.Config()
            config_vnic_4.portgroup = ''
            config_vnic_4.device = 'vmk4'
            config_vnic_4.changeOperation = 'edit'
            config_vnic_4.spec = vim.host.VirtualNic.Specification()
            config_vnic_4.spec.distributedVirtualPort = vim.dvs.PortConnection()
            config_vnic_4.spec.distributedVirtualPort.switchUuid = dvs.uuid
            config_vnic_4.spec.distributedVirtualPort.portgroupKey = pg.key

        # Edit proxy switch to add vmnic0 and vmnic1
        if "DVUplinks" in pg_name:
            config_proxySwitch_0 = vim.host.HostProxySwitch.Config()
            config_proxySwitch_0.uuid = dvs.uuid
            config_proxySwitch_0.changeOperation = 'edit'
            config_proxySwitch_0.spec = vim.host.HostProxySwitch.Specification()
            config_proxySwitch_0.spec.backing = vim.dvs.HostMember.PnicBacking()
            config_proxySwitch_0_spec_backing_pnicSpec_0 = vim.dvs.HostMember.PnicSpec()
            config_proxySwitch_0_spec_backing_pnicSpec_0.pnicDevice = 'vmnic0'
            config_proxySwitch_0_spec_backing_pnicSpec_0.uplinkPortgroupKey = pg.key
            config_proxySwitch_0_spec_backing_pnicSpec_1 = vim.dvs.HostMember.PnicSpec()
            config_proxySwitch_0_spec_backing_pnicSpec_1.pnicDevice = 'vmnic1'
            config_proxySwitch_0_spec_backing_pnicSpec_1.uplinkPortgroupKey = pg.key
            config_proxySwitch_0.spec.backing.pnicSpec = [config_proxySwitch_0_spec_backing_pnicSpec_0,
            config_proxySwitch_0_spec_backing_pnicSpec_1]
            config.proxySwitch = [config_proxySwitch_0]

    # Add the config specs on the host network system
    config.vnic = [vnic for vnic in [config_vnic_0, config_vnic_1, config_vnic_2, config_vnic_3, config_vnic_4] if vnic is not None]

    if config_proxySwitch_0 is not None:
        config.proxySwitch = [config_proxySwitch_0]

    changeMode = 'modify'

    # Update host network system
    try:
        network_system.UpdateNetworkConfig(config, changeMode)
        logging.info(f"Updated host {esxi_host_object.name} network configuration successfully.")
    except Exception as e:
        logging.error(f"Failed to update host {esxi_host_object.name} network configuration: {e}")        

def main():
    _init_logging()

    # vCenter server details
    vc_username = 'administrator@vsphere.local'
    esxi_username = 'opc'
    
    vcenter_ip, vc_password, esxi_password, datacenter_name, cluster_name = user_input()
    
    args = get_args()
    data = read_json_file(args.file_name)

    if data:
        esxi_hosts, vlan_details = process_ocvs_info(data)
    
        # Setup the SSL context
        vc_context = ssl.create_default_context()
        vc_context.check_hostname = False
        vc_context.verify_mode = ssl.CERT_NONE

        # Connect to VC
        vc, si = connect_to_vcenter(vcenter_ip, vc_username, vc_password, vc_context)

        try:
            # Get VC Datacenter
            datacenter = get_datacenter(vc, datacenter_name)
            logging.info("Datacenter name: %s", datacenter.name)

            # Create Cluster
            cluster_spec = vim.cluster.ConfigSpecEx()
            cluster = datacenter.hostFolder.CreateClusterEx(name=cluster_name, spec=cluster_spec)
            wait_for_vc_tasks_to_clear(si)
        
            # Create DVS
            dvs_name, dvs_display_name, dvs_uuid = create_dvs(datacenter, cluster_name)
            wait_for_vc_tasks_to_clear(si)
        
            #Create DPG
            dpg_management = create_dvs_port_group_management(dvs_name, cluster_name)
            wait_for_vc_tasks_to_clear(si)
            reconfigure_dvport_group_mgmt(dpg_management)
            wait_for_vc_tasks_to_clear(si)
        
            for vlan in vlan_details:
                vlan_name =vlan.get("VLAN Name")
                vlan_tag = vlan.get("VLAN Tag")
            
                if "Provisioning" in vlan_name:
                    dpg_provisioning = create_dvs_port_group_provisioning(dvs_name, cluster_name, vlan_tag)
                    wait_for_vc_tasks_to_clear(si)
                    dpg_port = dpg_provisioning
            
                if "Replication" in vlan_name:
                    dpg_replication = create_dvs_port_group_replication(dvs_name, cluster_name,vlan_tag)
                    wait_for_vc_tasks_to_clear(si)
                    dpg_port = dpg_replication
            
                if "vMotion" in vlan_name:
                    dpg_vmotion = create_dvs_port_group_vmotion(dvs_name, cluster_name,vlan_tag)
                    wait_for_vc_tasks_to_clear(si)
                    dpg_port = dpg_vmotion
            
                if "vSAN" in vlan_name:
                    dpg_vsan = create_dvs_port_group_vsan(dvs_name, cluster_name, vlan_tag)
                    wait_for_vc_tasks_to_clear(si)
                    dpg_port = dpg_vsan
            
            # Add ESXi host to the cluster
            for esxi_host in esxi_hosts:
                thumbprint = get_esxi_host_thumbprint(esxi_host, esxi_username, esxi_password, vc_context)
                spec = create_connect_spec(esxi_host, esxi_username, esxi_password, thumbprint)
                add_esxi = add_esxi_hosts_to_cluster(cluster, spec)
                wait_for_vc_tasks_to_clear(si)
                logging.info("%s successfully added to cluster", add_esxi)
                
            # Add ESXi host to dVS
            esxi_hosts_list, esxi_host_objects = list_esxi_hosts_in_cluster(cluster)
            for esxi_host_object in esxi_host_objects:
                esxi_host_name = esxi_host_object.name
                
                host_maintenance_mode(esxi_host_object)
                wait_for_vc_tasks_to_clear(si)
                
                esxi_host, dvs = retrieve_specific_host_and_dvs(si, datacenter, cluster, dvs_name, esxi_host_name)
                logging.info(f"Adding {esxi_host_name} to {dvs_name.name}")
                
                reconfigureDvs(dvs_name, esxi_host_name, si, datacenter, cluster)
                migrate_host_switch(datacenter, esxi_host_object,dvs_name, dpg_management, dpg_provisioning, dpg_replication, dpg_vmotion, dpg_vsan)
                wait_for_vc_tasks_to_clear(si)
            
            for vlan in vlan_details:
                vlan_name =vlan.get("VLAN Name")
                vlan_tag = vlan.get("VLAN Tag")
                vlan_cidr = vlan.get("VLAN CIDR Block")
            
                if "NSX VTEP" in vlan_name:
                    vtep_tag = vlan_tag
                    vtep_cidr = vlan_cidr
                    
            print("===========================================")
            print("Take note the following details for the NSX part.")
            print(f"Cluster Name: {cluster_name}")
            print(f"DVS Name: {dvs_display_name}")
            print(f"DVS UUID: {dvs_uuid}")
            print(f"VTEP Tag: {vtep_tag}")
            print(f"VTEP CIDR Block: {vtep_cidr}")
            print("===========================================")
            

        finally:
            # Disconnect from the vCenter server
            Disconnect(si)
            logging.info("Disconnected from vCenter Server")

if __name__ == '__main__':
    main()

