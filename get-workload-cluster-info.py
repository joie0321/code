import oci
import json

# Load OCI configuration from a file
config = oci.config.from_file("C:\\Users\opc\\_code-repo_scrap\\APIConfig.txt", "coeinfrastructure")

# Initialize service clients with default config file
ocvp_esxiclient = oci.ocvp.EsxiHostClient(config)
ocvp_clusterclient = oci.ocvp.ClusterClient(config)
compute_client = oci.core.ComputeClient(config)
virtual_network_client = oci.core.VirtualNetworkClient(config)

def user_input():
   cluster_id = input("Enter your OCVS Workload Cluster OCID: ")
   return cluster_id

def esxi_host_details(list_esxi_hosts_response):
    esxi_host_info = []
    if list_esxi_hosts_response.data:
        for esxi_host in list_esxi_hosts_response.data.items:
            esxi_host_info.append({"display_name": esxi_host.display_name, "id": esxi_host.compute_instance_id, "compartment_id": esxi_host.compartment_id})
    return esxi_host_info

def get_subnet_fqdn(virtual_network_client, subnet_id):
    try:
        subnet_details = virtual_network_client.get_subnet(subnet_id=subnet_id).data
        vcn_details = virtual_network_client.get_vcn(vcn_id=subnet_details.vcn_id).data
        return subnet_details.dns_label + "." + vcn_details.vcn_domain_name
    except oci.exceptions.ServiceError as e:
        print("Error:", e)

def filter_vnic_attachments_by_vlan_tag(compute_client, virtual_network_client, compartment_id, instance_id, vlan_tag=0):
    try:
        filtered_vnic_attachments = []
        list_vnic_attachments_response = compute_client.list_vnic_attachments(compartment_id=compartment_id, instance_id=instance_id)
        for vnic_attachment in list_vnic_attachments_response.data:
            if vnic_attachment.vlan_tag == vlan_tag:
                filtered_vnic_attachments.append(vnic_attachment)
        return filtered_vnic_attachments
    except oci.exceptions.ServiceError as e:
        print("Error:", e)
        
def list_sddc_network_configuration(ocvp_clusterclient, cluster_id):
    try:
        # Get the SDDC cluster details
        response = ocvp_clusterclient.get_cluster(cluster_id=cluster_id)
        sddc_cluster_network_config = response.data.network_configuration
        
        # Return list of VLAN IDs
        return [
            sddc_cluster_network_config.vsphere_vlan_id,
            sddc_cluster_network_config.provisioning_vlan_id,
            sddc_cluster_network_config.vmotion_vlan_id,
            sddc_cluster_network_config.replication_vlan_id,
            sddc_cluster_network_config.vsan_vlan_id,
            sddc_cluster_network_config.nsx_v_tep_vlan_id
            
        ]

    except oci.exceptions.ServiceError as e:
        print("Error:", e)
        return []

def get_vlan_tag(virtual_network_client, vlan_id):
    try:
        vlan = virtual_network_client.get_vlan(vlan_id=vlan_id).data
        return {"VLAN Name": vlan.display_name, "VLAN Tag": vlan.vlan_tag, "VLAN CIDR Block": vlan.cidr_block}
    except oci.exceptions.ServiceError as e:
        print(f"Error fetching VLAN tag for VLAN ID {vlan_id}: {e}")
        return {"VLAN Name": "N/A", "VLAN Tag": "N/A", "VLAN CIDR Block": "N/A"}


def main():
    #Call the user_input funtion to define the workload cluster id
    cluster_id = user_input()
    
    # Call the list_esxi_hosts method to retrieve ESXi hosts
    list_esxi_hosts_response = ocvp_esxiclient.list_esxi_hosts(
        cluster_id=cluster_id)
    
    # Call the esxi_host_details function to get the list of ESXi host display names and IDs
    esxi_host_info = esxi_host_details(list_esxi_hosts_response)

    # Create a dictionary to hold subnet domain FQDN information
    subnet_info = {}

    for host in esxi_host_info:
        # Compartment OCID and instance OCID
        compartment_id = host["compartment_id"]
        instance_id = host["id"]

        # Call the function to filter VNIC attachments by VLAN tag
        filtered_vnic_attachments = filter_vnic_attachments_by_vlan_tag(compute_client, virtual_network_client, compartment_id, instance_id, vlan_tag=0)

        # Store subnet domain FQDN information in the dictionary
        if filtered_vnic_attachments:
            subnet_info[host["display_name"]] = []
            for vnic_attachment in filtered_vnic_attachments:
                subnet_fqdn = get_subnet_fqdn(virtual_network_client, vnic_attachment.subnet_id)
                subnet_info[host["display_name"]].append({
                    "VNIC ID": vnic_attachment.vnic_id,
                    "VLAN Tag": vnic_attachment.vlan_tag,
                    "Subnet Domain FQDN": host["display_name"] + "." + subnet_fqdn
                })
                
     # List SDDC network configuration and get VLAN IDs
    list_of_vlans = list_sddc_network_configuration(ocvp_clusterclient, cluster_id)
    
    # List to hold VLAN tag details
    vlan_details = []

    # Get VLAN tags for each VLAN ID
    for vlan_id in list_of_vlans:
        vlan_info = get_vlan_tag(virtual_network_client, vlan_id)
        vlan_details.append(vlan_info)
        
    # Combine subnet_info and vlan_details into a single dictionary
    data_workload_cluster = {
        "Subnet Information": subnet_info,
        "SDDC VLAN Details": vlan_details
    }
    
    # Output combined data to a single JSON file
    with open("data_workload_cluster.json", "w") as json_file:
        json.dump(data_workload_cluster, json_file, indent=4)

if __name__ == "__main__":
    main()