This Python script automates the process of assigning display names and compartments for the boot and block volumes of an existing compute instance. Specifically designed to resolve post-migration issues with Oracle Cloud Infrastructure (OCI) migrated images using OCM (Oracle Cloud Migration) Tool RMS stack, the script addresses a challenge where a migrated image, when spun up using the OCM RMS stack, is assigned random names for its boot and block volumes. These names are difficult to correlate with the associated OCI compute instance. Additionally, the OCM RMS stack places the boot and block volumes in a migration compartment rather than the correct compartment.

The script performs the following steps:
Capture Configuration Details: Captures instance configuration and block storage/s details.

Modify the Boot and Block Volume Display Name: Modify the display names of the boot and block volume/s attached to the compute instance to match the compute instance name.

Update the Boot and Block Volume Compartment: Move the boot and block volume/s to the compartment where the compute instance resides.
