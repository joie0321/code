import requests
from requests.auth import HTTPBasicAuth
import json

def get_nsx_manager_credentials(data):
    """Get NSX Manager credentials and URL from the data."""
    nsx_manager = data['nsx_manager']
    nsx_manager_url = nsx_manager['url']
    nsx_username = nsx_manager['username']
    nsx_password = nsx_manager['password']
    return nsx_manager_url, nsx_username, nsx_password

def create_uplink_profile(data):
    """Create or update host switch profile using the NSX Manager credentials and profile data."""
    nsx_manager_url, nsx_username, nsx_password = get_nsx_manager_credentials(data)
    host_switch_profile = data['host_switch_profile']
    host_switch_profile_id = host_switch_profile['id']

    # API endpoint to create or update Hostswitch Profiles
    api_endpoint = f'{nsx_manager_url}/policy/api/v1/infra/host-switch-profiles/{host_switch_profile_id}'

    # Disable SSL warnings
    requests.packages.urllib3.disable_warnings()

    # Define the Hostswitch Profile data
    host_switch_profile_data = {
        "teaming": {
            "policy": "LOADBALANCE_SRC_MAC",
            "active_list": [
                {"uplink_name": "uplink-1", "uplink_type": "PNIC"},
                {"uplink_name": "uplink-2", "uplink_type": "PNIC"}
            ]
        },
        "transport_vlan": host_switch_profile['transport_vlan'],
        "resource_type": "PolicyUplinkHostSwitchProfile",
        "display_name": host_switch_profile['display_name']
    }

    # Headers for the request
    headers = {'Content-Type': 'application/json'}

    # Make the PUT request to the NSX Manager API
    response = requests.put(api_endpoint, auth=HTTPBasicAuth(nsx_username, nsx_password), headers=headers, data=json.dumps(host_switch_profile_data), verify=False)

    # Check if the request was successful
    if response.status_code == 200:
        response_json = response.json()
        print("Uplink Profile created or updated successfully.")
        return response_json.get('unique_id'), host_switch_profile['display_name']
    else:
        print(f"Failed to create or update Uplink Profile: {response.status_code} - {response.text}")
        return None, None

def create_vtep_ip_pool(data):
    """Create IP Pool using the NSX Manager credentials and IP pool data."""
    nsx_manager_url, nsx_username, nsx_password = get_nsx_manager_credentials(data)
    ip_pool_data = data['ip_pool']

    # API endpoint to create IP Pools
    api_endpoint = f'{nsx_manager_url}/api/v1/pools/ip-pools'

    # Disable SSL warnings
    requests.packages.urllib3.disable_warnings()

    # Headers for the request
    headers = {'Content-Type': 'application/json'}

    # Make the POST request to the NSX Manager API
    response = requests.post(api_endpoint, auth=HTTPBasicAuth(nsx_username, nsx_password), headers=headers, data=json.dumps(ip_pool_data), verify=False)

    # Check if the request was successful
    if response.status_code == 201:
        response_json = response.json()
        print("VTEP IP Pool created successfully.")
        return response_json.get('id'), ip_pool_data.get('display_name')
    else:
        print(f"Failed to create VTEP IP Pool: {response.status_code} - {response.text}")
        return None, None

def get_transport_zone_ids(data):
    """Get the IDs of overlay and VLAN transport zones."""
    nsx_manager_url, nsx_username, nsx_password = get_nsx_manager_credentials(data)

    # Disable SSL warnings
    requests.packages.urllib3.disable_warnings()

    # API endpoint to get transport zones
    api_endpoint = f'{nsx_manager_url}/api/v1/transport-zones'

    # Make the GET request to the NSX Manager API
    response = requests.get(api_endpoint, auth=HTTPBasicAuth(nsx_username, nsx_password), verify=False)

    # Check if the request was successful
    if response.status_code == 200:
        transport_zones = response.json().get('results', [])
        tz_ids = {}
        for tz in transport_zones:
            tz_name = tz.get('display_name', '')
            tz_id = tz.get('id', '')
            if tz_name == 'Overlay-TZ':
                tz_ids['Overlay-TZ'] = tz_id
            elif tz_name == 'VLAN-TZ':
                tz_ids['VLAN-TZ'] = tz_id
        return tz_ids
    else:
        print(f"Failed to retrieve transport zones: {response.status_code} - {response.text}")
        return None

def create_transport_node_profile(data, ip_pool_id, overlay_tz_id, vlan_tz_id, uplink_display_name):
    """Create Transport Node Profile using the NSX Manager credentials and profile data."""
    nsx_manager_url, nsx_username, nsx_password = get_nsx_manager_credentials(data)
    transport_node_profile = data['transport_node_profile']
    transport_node_profile_name = transport_node_profile['display_name']
    transport_node_profile_id = transport_node_profile['id']
    dvs_name = transport_node_profile['dvs_name']
    dvs_uuid = transport_node_profile['dvs_uuid']

    # API endpoint to create Transport Node Profile
    api_endpoint = f'{nsx_manager_url}/policy/api/v1/infra/host-transport-node-profiles/{transport_node_profile_id}'

    # Disable SSL warnings
    requests.packages.urllib3.disable_warnings()

    # Define the Transport Node Profile data
    transport_node_profile_data = {
        "display_name": transport_node_profile_name,
        "description": transport_node_profile_name,
        "host_switch_spec": {
            "host_switches": [
                {
                    "host_switch_id": dvs_uuid,
                    "host_switch_name": dvs_name,
                    "host_switch_type": "VDS",
                    "host_switch_mode": "STANDARD",
                    "ip_assignment_spec": {
                        "resource_type": "StaticIpPoolSpec",
                        "ip_pool_id": f'/infra/host-switch-profiles/{ip_pool_id}'
                    },
                    "uplinks": [
                        {"vds_uplink_name": "uplink-vmnic0", "uplink_name": "uplink-1"},
                        {"vds_uplink_name": "uplink-vmnic1", "uplink_name": "uplink-2"}
                    ],
                    "transport_zone_endpoints": [
                        {"transport_zone_id": f'/infra/sites/default/enforcement-points/default/transport-zones/{overlay_tz_id}'},
                        {"transport_zone_id": f'/infra/sites/default/enforcement-points/default/transport-zones/{vlan_tz_id}'}
                    ],
                    "host_switch_profile_ids": [
                        {"key": "UplinkHostSwitchProfile", "value": f'/infra/host-switch-profiles/{uplink_display_name}'}
                    ]
                }
            ],
            "resource_type": "StandardHostSwitchSpec"
        }
    }

    # Headers for the request
    headers = {'Content-Type': 'application/json'}

    # Make the PUT request to the NSX Manager API
    response = requests.put(api_endpoint, auth=HTTPBasicAuth(nsx_username, nsx_password), headers=headers, data=json.dumps(transport_node_profile_data), verify=False)

    # Check if the request was successful
    if response.status_code in [200, 201]:
        response_json = response.json()
        print("Transport Node Profile created successfully.")
        return response_json
    else:
        print(f"Failed to create Transport Node Profile: {response.status_code} - {response.text}")
        return None

def get_cluster_id(data):
    """Retrieve the ID of a specific cluster."""
    nsx_manager_url, nsx_username, nsx_password = get_nsx_manager_credentials(data)
    transport_node_collection = data['transport_node_collection']
    cluster_name = transport_node_collection['cluster_name']

    clusters_url = f'{nsx_manager_url}/api/v1/fabric/compute-collections'
    response = requests.get(clusters_url, auth=HTTPBasicAuth(nsx_username, nsx_password), verify=False)

    if response.status_code == 200:
        clusters = response.json().get('results', [])
        for cluster in clusters:
            if cluster['display_name'] == cluster_name:
                return cluster['external_id']
        print("Cluster with the specified name not found.")
        return None
    else:
        print(f"Failed to fetch clusters: {response.text}")
        return None

def get_transport_node_profile_id(data):
    """Retrieve the ID of a specific transport node profile."""
    nsx_manager_url, nsx_username, nsx_password = get_nsx_manager_credentials(data)
    transport_node_profile = data['transport_node_profile']
    transport_node_profile_name = transport_node_profile['display_name']

    profiles_url = f'{nsx_manager_url}/policy/api/v1/infra/host-transport-node-profiles'
    response = requests.get(profiles_url, auth=HTTPBasicAuth(nsx_username, nsx_password), verify=False)

    if response.status_code == 200:
        transport_node_profiles = response.json().get('results', [])
        for profile in transport_node_profiles:
            if profile['display_name'] == transport_node_profile_name:
                return profile['unique_id']
        print("Transport node profile with the specified name not found.")
        return None
    else:
        print(f"Failed to fetch transport node profile: {response.text}")
        return None

def create_transport_node_collection(data, cluster_id, tnp_id):
    """Create a Transport Node Collection using the NSX Manager credentials and provided IDs."""
    nsx_manager_url, nsx_username, nsx_password = get_nsx_manager_credentials(data)
    transport_node_collection = data['transport_node_collection']
    transport_node_collection_name = transport_node_collection['display_name']

    url = f'{nsx_manager_url}/api/v1/transport-node-collections'

    # Data for creating the transport node collection
    data = {
        "resource_type": "TransportNodeCollection",
        "display_name": transport_node_collection_name,
        "compute_collection_id": cluster_id,
        "transport_node_profile_id": tnp_id
    }

    headers = {'Content-Type': 'application/json'}

    response = requests.post(url, auth=HTTPBasicAuth(nsx_username, nsx_password), headers=headers, data=json.dumps(data), verify=False)

    if response.status_code == 201:
        print("Transport node collection created successfully.")
        return response.json()
    else:
        print("Failed to create transport node collection.")
        return None

def main():
    json_file = 'nsx_input.json'  # Path to your JSON file

    # Load settings from the JSON file
    with open(json_file, 'r') as file:
        data = json.load(file)

    uplink_profile_id, uplink_display_name = create_uplink_profile(data)
    if uplink_profile_id is None:
        print("Error creating uplink profile. Exiting.")
        return
    
    ip_pool_id, ip_pool_display_name = create_vtep_ip_pool(data)
    if ip_pool_id is None:
        print("Error creating VTEP IP Pool. Exiting.")
        return

    tz_ids = get_transport_zone_ids(data)
    if not tz_ids:
        print("Error retrieving transport zone IDs. Exiting.")
        return
    
    overlay_tz_id = tz_ids.get('Overlay-TZ')
    vlan_tz_id = tz_ids.get('VLAN-TZ')

    transport_node_profile = create_transport_node_profile(data, ip_pool_id, overlay_tz_id, vlan_tz_id, uplink_display_name)
    if transport_node_profile is None:
        print("Error creating transport node profile. Exiting.")
        return

    cluster_id = get_cluster_id(data)
    if cluster_id is None:
        print("Error retrieving cluster ID. Exiting.")
        return

    tnp_id = get_transport_node_profile_id(data)
    if tnp_id is None:
        print("Error retrieving transport node profile ID. Exiting.")
        return

    transport_node_collection = create_transport_node_collection(data, cluster_id, tnp_id)
    if transport_node_collection is None:
        print("Error creating transport node collection. Exiting.")
        return

if __name__ == "__main__":
    main()
