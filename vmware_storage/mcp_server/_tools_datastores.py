"""Datastores MCP registrations sharing the server runtime.

The facade owns connection, error and Policy state; this module creates no second
server or connection manager. Returned aliases preserve the original Python API.
"""

from typing import Optional


def register_tools(mcp, runtime):
    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def list_all_datastores(
        target: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """[READ] List all datastores with capacity, usage percentage, and accessibility.

        Use this first for the datastore_name that browse_datastore and
        scan_datastore_images require.

        Returns the list envelope: 'items' holds one row per datastore, and
        'returned'/'total'/'truncated' state whether the listing is complete.
        Every datastore is enumerated before limit is applied, so total is the
        real count and truncated is true only when limit cut rows off.

        Args:
            target: Optional vCenter/ESXi target name from config.
            limit: Max datastores to return (None = all).
        """
        try:
            si = runtime._get_connection(target)
            return runtime.list_datastores(si, limit=limit)
        except Exception as e:
            runtime.logger.error("list_all_datastores failed: %s", e)
            return {"error": runtime._safe_error(e, "storage"), "hint": runtime._hint_for(e)}

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def get_datastore_vm_usage(
        datastore_name: str,
        top_n: int = 10,
        target: Optional[str] = None,
    ) -> dict:
        """[READ] Which VMs take the space on one datastore, largest first.

        Use this for "is this datastore filling up, and who is using it?".
        datastore_name comes from list_all_datastores. Reads vCenter's own per-VM
        accounting (storage.perDatastoreUsage), so it is space on disk, not the
        provisioned VMDK sizes browse_datastore shows.

        Returns {datastore: {name, type, total_gb, used_gb, free_gb, usage_pct},
        items, returned, total, truncated, attributed_gb, unattributed_gb,
        unmeasured_vms, note}. Each item: name, vm_id, template, committed_gb (on
        disk), uncommitted_gb (thin headroom it may still grow into), unshared_gb,
        measured_at. unattributed_gb is used space no VM accounts for: unregistered
        VMs, orphaned disks, ISOs; find those files with browse_datastore.
        unmeasured_vms list the datastore but have no usage row, so their space is
        unknown, not zero.

        Args:
            datastore_name: Datastore name exactly as shown in vCenter.
            top_n: How many VMs to return, largest committed first (default 10).
            target: Optional vCenter/ESXi target name from config.
        """
        try:
            si = runtime._get_connection(target)
            return runtime.datastore_usage.datastore_vm_usage(si, datastore_name, top_n=top_n)
        except Exception as e:
            runtime.logger.error("get_datastore_vm_usage failed: %s", e)
            return {"error": runtime._safe_error(e, "storage"), "hint": runtime._hint_for(e)}

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def browse_datastore(
        datastore_name: str,
        path: str = "",
        pattern: str = "*",
        target: Optional[str] = None,
    ) -> dict:
        """[READ] Search a datastore folder and all its subfolders for files.

        Use this for arbitrary files or a glob; prefer scan_datastore_images for
        deployable images. datastore_name comes from list_all_datastores.

        Returns the list envelope: 'items' holds one row per file, and
        'returned'/'total'/'truncated' state whether the listing is complete.
        All matches are returned, so truncated is always false.

        Args:
            datastore_name: Datastore name.
            path: Datastore-relative folder to start from (empty for root); a
                leading '/' or '..' is refused. The search is recursive,
                so the root returns matches from every folder; each row's
                ds_path is "[ds] dir/file".
            pattern: Glob pattern to filter files (e.g. "*.ova", "*.iso").
            target: Optional vCenter/ESXi target name from config.
        """
        try:
            si = runtime._get_connection(target)
            return runtime.datastore_browser.browse_datastore(
                si, datastore_name, path=path, pattern=pattern
            )
        except Exception as e:
            runtime.logger.error("browse_datastore failed: %s", e)
            return {"error": runtime._safe_error(e, "storage"), "hint": runtime._hint_for(e)}

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def scan_datastore_images(
        datastore_name: str,
        path: str = "",
        target: Optional[str] = None,
    ) -> dict:
        """[READ] Scan a datastore for deployable images (OVA, ISO, OVF, VMDK).

        Use this for a live scan of one datastore; prefer list_cached_images for
        a cached answer. datastore_name comes from list_all_datastores.

        Returns the list envelope: 'items' holds one row per image, and
        'returned'/'total'/'truncated' state whether the listing is complete.
        All patterns are browsed, so truncated is always false.

        Args:
            datastore_name: Datastore name.
            path: Datastore-relative subdirectory (empty for root); a leading '/'
                or '..' is refused. The images found replace this datastore's
                cached entries under that folder, for list_cached_images.
            target: Optional vCenter/ESXi target name from config.
        """
        try:
            mgr = runtime._get_conn_mgr()
            name = mgr.resolve_target_name(target)
            si = mgr.connect(name)
            return runtime.datastore_browser.scan_images(si, datastore_name, path=path, target=name)
        except Exception as e:
            runtime.logger.error("scan_datastore_images failed: %s", e)
            return {"error": runtime._safe_error(e, "storage"), "hint": runtime._hint_for(e)}

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def list_cached_images(
        image_type: Optional[str] = None,
        datastore: Optional[str] = None,
        target: Optional[str] = None,
    ) -> dict:
        """[READ] List deployable images (OVA/OVF/ISO/VMDK) from the local cache registry — instant, no vCenter connection or datastore I/O.

        Reads ~/.vmware-storage/image_registry.json, which scan_datastore_images
        fills; results may be stale, and are empty until a scan has run. For a live
        listing use scan_datastore_images instead.
        Returns the list envelope: 'items' holds {target, datastore, name, ds_path,
        size_mb, type, modified} and is empty if nothing matches, while
        'returned'/'total'/'truncated' state whether the listing is complete. The
        whole registry is filtered in memory, so truncated is always false.

        Args:
            image_type: Filter by file extension without the dot, e.g. "ova",
                "iso", "ovf", "vmdk" (case-insensitive). Omit for all types.
            datastore: Filter by exact datastore name. Omit for all datastores.
            target: Only rows scanned from this vCenter/ESXi target (a name from
                config, as scan_datastore_images takes; an unknown name is refused
                with the configured ones). Rows cached before rows recorded their
                target appear only when this is omitted. Omit for every target.
        """
        try:
            # Resolved the way scan_datastore_images records it; this reads
            # config.yaml only, it does not connect.
            name = runtime._get_conn_mgr().resolve_target_name(target) if target else None
        except Exception as e:
            runtime.logger.error("list_cached_images failed: %s", e)
            return {"error": runtime._safe_error(e, "storage"), "hint": runtime._hint_for(e)}
        try:
            return runtime.datastore_browser.list_images(
                image_type=image_type, datastore=datastore, target=name
            )
        except Exception as e:
            runtime.logger.error("list_cached_images failed: %s", e)
            return {
                "error": runtime._safe_error(e, "storage"),
                "hint": "Local image registry may be missing or corrupt — re-run scan_datastore_images to rebuild it (no vCenter connectivity is involved here).",
            }

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="medium")
    def enable_iscsi(
        host_name: str,
        confirm: bool = False,
        target: Optional[str] = None,
    ) -> dict:
        """[WRITE] Enable the software iSCSI adapter (vmhba) on an ESXi host.

        Required prerequisite before add_iscsi_target. Without
        confirm=True this only previews: it returns blast_radius (the host, its
        current adapters, whether the software adapter already exists) and changes
        nothing. Show it to the user. Do not set confirm=True on your own because
        the user asked earlier — they have not seen the preview yet. Already
        enabled returns action "noop" with the HBA device and IQN. Non-disruptive
        (no reboot, no impact on existing datastores). Refused when the host's
        storage view cannot be read. Returns a dict; check state first with
        get_iscsi_status.

        Args:
            host_name: ESXi host name exactly as shown in vCenter inventory
                (FQDN or IP). Errors if not found.
            confirm: False (default) returns the blast radius and changes nothing. True applies it.
            target: Optional vCenter/ESXi target name from config.
        """
        decision = runtime._gate.decide(confirm)
        try:
            si = runtime._get_connection(target)
            radius = runtime._gate.measure_enable(si, host_name)
            if radius["already_enabled"]:
                adapter = radius["adapter"]
                return {
                    "action": "noop",
                    "result": f"Software iSCSI is already enabled on host '{host_name}' "
                    f"(HBA: {adapter['device']}, IQN: {adapter['iqn']}).",
                    "blast_radius": radius,
                }
            if not decision.act:
                return runtime._preview(radius)
            runtime._gate.refuse_if_blocked("enable_iscsi", host_name, radius)
            result = runtime.enable_software_iscsi(si, host_name)
            runtime._safe_audit(
                target=target or "default",
                operation="iscsi_enable",
                resource=host_name,
                parameters={"host_name": host_name},
                result=result,
            )
            return {"action": "enabled", "result": result, "blast_radius": radius}
        except Exception as e:
            runtime.logger.error("enable_iscsi failed: %s", e)
            return runtime._error_reply(e, "storage")

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def get_iscsi_status(
        host_name: str,
        target: Optional[str] = None,
    ) -> dict:
        """[READ] Get the software iSCSI adapter state and configured send targets for an ESXi host.

        Returns {host, enabled, hba_device, iqn, send_targets: [{address, port}]};
        when the adapter is disabled, enabled=false with null device/IQN and an
        empty target list. Use this before enable_iscsi /
        add_iscsi_target / remove_iscsi_target to check
        prerequisites, and afterwards to verify the change took effect.

        Args:
            host_name: ESXi host name exactly as shown in vCenter inventory
                (FQDN or IP). Errors if not found. No host listing here — get
                names from vmware-monitor list_esxi_hosts.
            target: Optional vCenter/ESXi target name from config.
        """
        try:
            si = runtime._get_connection(target)
            return runtime._ops_get_iscsi_status(si, host_name)
        except Exception as e:
            runtime.logger.error("get_iscsi_status failed: %s", e)
            return {"error": runtime._safe_error(e, "storage"), "hint": runtime._hint_for(e)}

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(
        risk_level="medium",
        undo=runtime._acted_undo(
            "target_added",
            "remove_iscsi_target",
            "Inverse of add_iscsi_target: remove the iSCSI send target that was added.",
        ),
    )
    def add_iscsi_target(
        host_name: str,
        address: str,
        port: int = 3260,
        confirm: bool = False,
        target: Optional[str] = None,
    ) -> dict:
        """[WRITE] Add an iSCSI send (dynamic discovery) target to an ESXi host's software iSCSI adapter, then automatically rescan all HBAs and VMFS volumes to discover new LUNs.

        Without confirm=True this only previews: it returns blast_radius (the
        adapter, the send targets configured now, and the adapters the rescan
        touches) and changes nothing. Show it to the user. Do not set confirm=True
        on your own because the user asked earlier — they have not seen the
        preview yet. Refused when software iSCSI is not enabled (run
        enable_iscsi first) or the host's storage view cannot be read. A
        duplicate address:port returns action "noop". No separate rescan_storage
        call is needed afterwards. Returns a dict.

        Args:
            host_name: ESXi host name as shown in vCenter inventory.
            address: iSCSI portal IP address (IPv4/IPv6 literal; hostnames are
                rejected with a validation error).
            port: iSCSI TCP port, 1-65535 (default 3260).
            confirm: False (default) returns the blast radius and changes nothing. True applies it.
            target: Optional vCenter/ESXi target name from config.
        """
        decision = runtime._gate.decide(confirm)
        try:
            si = runtime._get_connection(target)
            radius = runtime._gate.measure_add_target(si, host_name, address, port)
            if radius["already_configured"]:
                return {
                    "action": "noop",
                    "result": f"iSCSI target {address}:{port} already configured on '{host_name}'.",
                    "blast_radius": radius,
                }
            if not decision.act:
                return runtime._preview(radius)
            runtime._gate.refuse_if_blocked("add_iscsi_target", host_name, radius)
            result = runtime._ops_add_iscsi_target(si, host_name, address, port)
            if runtime._iscsi_config.ALREADY_CONFIGURED in result:
                # Added by someone else after the gate read: this call changed
                # nothing, so it must not record an undo that would remove it.
                return {"action": "noop", "result": result, "blast_radius": radius}
            runtime._safe_audit(
                target=target or "default",
                operation="iscsi_add_target",
                resource=host_name,
                parameters={"host_name": host_name, "address": address, "port": port},
                result=result,
            )
            return {"action": "target_added", "result": result, "blast_radius": radius}
        except Exception as e:
            runtime.logger.error("add_iscsi_target failed: %s", e)
            return runtime._error_reply(e, "storage")

    return (
        list_all_datastores,
        get_datastore_vm_usage,
        browse_datastore,
        scan_datastore_images,
        list_cached_images,
        enable_iscsi,
        get_iscsi_status,
        add_iscsi_target,
    )
