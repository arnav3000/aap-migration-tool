"""
AAP 2.4 to 2.6 Sizing Calculator
Calculates recommended container resources for AAP 2.6 based on AAP 2.4 VM metrics
Using Official Red Hat Excel Reference Formulas
"""

import math
from typing import Any


class AAP26SizingCalculator:
    """
    Calculates sizing recommendations for AAP 2.6 container deployment
    based on AAP 2.4 VM metrics and workload characteristics.

    Uses official Red Hat formulas from Excel reference:
    - Time-based concurrency: forks = hosts × jobs_per_hostday × job_duration / allowed_hours
    - Memory: (forks × 100MB) / 1024 + 2GB × nodes
    - CPU (AVG): 2 × nodes + forks / 4 / 10
    - Control plane: AVERAGE of event processing AND job management
    """

    # Red Hat official benchmarked constants (from Excel reference)
    MEMORY_PER_FORK_MB = 100  # Memory consumed per parallel fork
    FORKS_PER_CPU = 4  # Number of forks one CPU core can handle
    EVENT_SIZE_KB = 2  # Event size in database (debug mode)
    FACTS_SIZE_PER_HOST_KB = 50  # Inventory facts per host
    EE_AVERAGE_SIZE_MB = 1600  # Execution Environment image size
    CONTROLLER_EVENTS_PER_SEC = 400  # Events processed per second
    MEMORY_PER_EVENT_FORK_MB = 0.0124  # Memory for event processing
    CPU_PER_EVENT_FORK = 0.00011  # CPU for event processing
    API_CALLS_PER_CONTROLLER = 100  # Concurrent API calls supported

    # Verbosity level impact on events per task (CRITICAL for control plane sizing)
    VERBOSITY_EVENTS_PER_TASK = {
        0: 4,  # Minimal output (-60% events vs baseline)
        1: 6,  # Normal/default (baseline)
        2: 12,  # Verbose (+100% events)
        3: 34,  # Debug (+467% events)
        4: 50,  # Connection debug (+733% events)
    }

    # Peak concurrency patterns (CRITICAL for realistic sizing)
    PEAK_CONCURRENCY_MULTIPLIERS = {
        "distributed_24x7": 1.0,  # Even distribution 24/7
        "business_hours": 2.5,  # 9-5 concentration (8 hours)
        "batch_window": 10.0,  # 2-4 hour batch window
        "mixed": 1.5,  # Mixed pattern
    }

    # Standard node base reservations
    BASE_MEMORY_GB_PER_NODE = 2  # Base memory reservation per node
    BASE_CPU_PER_EXECUTION_NODE = 2  # Base CPU per execution node
    BASE_CPU_PER_CONTROL_NODE = 1.6  # Base CPU per control node

    # Standard node specifications
    STANDARD_NODE_SPEC = {"cpu": 4, "memory_gb": 16, "disk_gb": 128, "iops": 3000}

    # Input validation ranges
    VALIDATION_RANGES: dict[str, dict[str, int | float]] = {
        "tasks_per_job": {"min": 1, "max": 500, "typical_min": 5, "typical_max": 200},
        "job_duration_hours": {"min": 0.01, "max": 24, "typical_min": 0.02, "typical_max": 2.0},
        "forks_observed": {"min": 1, "max": 500, "typical_min": 1, "typical_max": 50},
        "managed_hosts": {"min": 1, "max": 1000000, "typical_min": 100, "typical_max": 100000},
        "playbooks_per_day_peak": {
            "min": 1,
            "max": 10000000,
            "typical_min": 100,
            "typical_max": 500000,
        },
    }

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def validate_input(self, param_name: str, value: float, context: str = "") -> list[str]:
        """
        Validate input parameter and return warnings if value seems unusual.

        Returns list of warning messages.
        """
        warnings: list[str] = []

        if param_name not in self.VALIDATION_RANGES:
            return warnings

        ranges = self.VALIDATION_RANGES[param_name]

        # Check absolute limits
        if value < ranges["min"] or value > ranges["max"]:
            warnings.append(
                f"⚠️ {param_name} value ({value}) is outside valid range "
                f"({ranges['min']}-{ranges['max']}). {context}"
            )

        # Check typical ranges (warnings, not errors)
        elif value < ranges["typical_min"] or value > ranges["typical_max"]:
            warnings.append(
                f"ℹ️ {param_name} value ({value}) is outside typical range "
                f"({ranges['typical_min']}-{ranges['typical_max']}). "
                f"This may indicate unusual workload. {context}"
            )

        return warnings

    def validate_results(
        self, execution: dict[str, Any], controller: dict[str, Any], database: dict[str, Any]
    ) -> list[str]:
        """
        Validate calculation results and return warnings for unusual patterns.

        Returns list of warning messages.
        """
        warnings = []

        # Control plane shouldn't dominate execution plane
        if controller["total_memory_gb"] > execution["total_memory_gb"] * 2:
            warnings.append(
                f"⚠️ HIGH SEVERITY: Control plane memory ({controller['total_memory_gb']} GB) "
                f"> 2× execution plane ({execution['total_memory_gb']} GB). "
                f"This is unusual. Verify 'tasks per job' ({controller.get('events_per_task', 'N/A')} events/task) "
                f"and 'verbosity level' ({controller.get('verbosity_level', 'N/A')}) inputs."
            )

        # Database shouldn't be too small for production
        if database["storage_gb"] < 100:
            warnings.append(
                f"ℹ️ Database storage ({database['storage_gb']} GB) < 100 GB seems low for production. "
                f"Verify retention period and job volume. Consider 2x buffer for growth."
            )

        # Too many execution pods might indicate incorrect peak pattern
        if execution["execution_pods"] > 50:
            warnings.append(
                f"ℹ️ {execution['execution_pods']} execution pods is very large. "
                f"Peak pattern: {execution.get('peak_pattern', 'unknown')} ({execution.get('peak_multiplier', '?')}x). "
                f"Consider automation mesh for distributed execution."
            )

        # Execution pods too few for high workload
        if execution["execution_pods"] < 2 and execution.get("forks_needed", 0) > 100:
            warnings.append(
                f"⚠️ Only {execution['execution_pods']} execution pods for {execution.get('forks_needed', 0)} forks "
                f"may cause resource contention. Consider adding more pods."
            )

        return warnings

    def calculate_execution_forks(
        self,
        number_of_hosts: int,
        jobs_per_host_per_day: float,
        job_duration_hours: float,
        allowed_hours_per_day: int = 24,
        peak_pattern: str = "distributed_24x7",
    ) -> float:
        """
        Calculate needed forks for parallel execution using time-based formula.

        Formula: forks = (hosts × jobs_per_host_per_day × job_duration_hours) / allowed_hours_per_day
                        × peak_pattern_multiplier

        This accounts for how many jobs need to run concurrently based on:
        - Total daily job volume
        - How long each job takes
        - Time window available for execution
        - Peak concurrency pattern (CRITICAL for accuracy!)

        Peak patterns:
        - distributed_24x7: Even distribution (multiplier: 1.0)
        - business_hours: 9-5 concentration (multiplier: 2.5)
        - batch_window: 2-4 hour batch (multiplier: 10.0)
        - mixed: Mixed pattern (multiplier: 1.5)
        """
        base_forks = (
            number_of_hosts * jobs_per_host_per_day * job_duration_hours / allowed_hours_per_day
        )

        # Apply peak pattern multiplier (CRITICAL for realistic sizing)
        multiplier = self.PEAK_CONCURRENCY_MULTIPLIERS.get(peak_pattern, 1.0)
        forks_needed = base_forks * multiplier

        return forks_needed

    def calculate_execution_memory(self, forks_needed: float, number_of_nodes: int) -> float:
        """
        Calculate execution node memory using Red Hat's fork-based formula.

        Formula: memory_gb = (forks_needed × 100MB) / 1024 + (2GB × number_of_nodes)
        """
        memory_gb = (forks_needed * self.MEMORY_PER_FORK_MB) / 1024
        memory_total_gb = memory_gb + (self.BASE_MEMORY_GB_PER_NODE * number_of_nodes)
        return memory_total_gb

    def calculate_execution_cpu_avg(self, forks_needed: float, number_of_nodes: int) -> float:
        """
        Calculate execution node CPU using AVERAGED formula (realistic).

        Formula: cpu_avg = 2 × number_of_nodes + forks_needed / 4 / 10

        The /10 divisor accounts for average vs peak load.
        Do NOT use the MAX formula (forks / 4) as it's typically too high.
        """
        cpu_avg = (
            self.BASE_CPU_PER_EXECUTION_NODE * number_of_nodes
            + forks_needed / self.FORKS_PER_CPU / 10
        )
        return cpu_avg

    def calculate_event_forks(
        self,
        number_of_hosts: int,
        jobs_per_host_per_day: float,
        tasks_per_job: int,
        job_duration_hours: float,
        allowed_hours_per_day: int = 24,
        verbosity_level: int = 1,
    ) -> float:
        """
        Calculate event forks that need to be processed in parallel.

        Formula: event_forks = hosts × jobs_per_host_per_day × tasks_per_job ×
                              events_per_task × job_duration_hours / allowed_hours_per_day

        Verbosity level significantly impacts event generation:
        - Level 0 (minimal): 4 events/task
        - Level 1 (normal): 6 events/task (default)
        - Level 2 (verbose): 12 events/task
        - Level 3 (debug): 34 events/task
        - Level 4 (connection): 50 events/task
        """
        events_per_task = self.VERBOSITY_EVENTS_PER_TASK.get(verbosity_level, 6)

        event_forks = (
            number_of_hosts
            * jobs_per_host_per_day
            * tasks_per_job
            * events_per_task
            * job_duration_hours
            / allowed_hours_per_day
        )
        return event_forks

    def calculate_control_memory_for_events(
        self, event_forks: float, number_of_nodes: int
    ) -> float:
        """
        Calculate control node memory for event processing.

        Formula: memory_gb = (event_forks × 0.0124MB) / 1024 + (2GB × number_of_nodes)
        """
        memory_for_events_mb = event_forks * self.MEMORY_PER_EVENT_FORK_MB
        memory_gb = memory_for_events_mb / 1024 + (self.BASE_MEMORY_GB_PER_NODE * number_of_nodes)
        return memory_gb

    def calculate_control_cpu_for_events_avg(
        self, event_forks: float, number_of_nodes: int
    ) -> float:
        """
        Calculate control node CPU for event processing (AVERAGED).

        Formula: cpu_avg = event_forks × 0.00011 / 10 + (1.6 × number_of_nodes)
        """
        cpu_avg = (
            event_forks * self.CPU_PER_EVENT_FORK / 10
            + self.BASE_CPU_PER_CONTROL_NODE * number_of_nodes
        )
        return cpu_avg

    def calculate_control_memory_for_jobs(
        self, forks_for_jobs: float, number_of_nodes: int
    ) -> float:
        """
        Calculate control node memory for job management.

        Formula: memory_gb = (forks_for_jobs × 100MB) / 1024 + (2GB × number_of_nodes)
        """
        memory_gb = (forks_for_jobs * self.MEMORY_PER_FORK_MB) / 1024
        memory_total_gb = memory_gb + (self.BASE_MEMORY_GB_PER_NODE * number_of_nodes)
        return memory_total_gb

    def calculate_control_cpu_for_jobs_avg(
        self, forks_for_jobs: float, number_of_nodes: int
    ) -> float:
        """
        Calculate control node CPU for job management (AVERAGED).

        Formula: cpu_avg = 2 × number_of_nodes + forks_for_jobs / 4 / 10
        """
        cpu_avg = (
            self.BASE_CPU_PER_EXECUTION_NODE * number_of_nodes
            + forks_for_jobs / self.FORKS_PER_CPU / 10
        )
        return cpu_avg

    def calculate_database_storage(
        self,
        number_of_hosts: int,
        jobs_per_host_per_day: float,
        tasks_per_job: int,
        days_to_keep_jobs: int,
    ) -> dict[str, Any]:
        """
        Calculate database storage requirements.

        Formula:
        - Facts: hosts × 50KB / 1024 (MB)
        - Inventory: hosts × 50KB / 1024 (MB)
        - Jobs: hosts × jobs_per_host_per_day × tasks_per_job × events_per_task ×
                days_to_keep_jobs × 2KB / 1024 (MB)
        - Total: (Facts + Inventory + Jobs) / 1024 (GB)
        """
        # Facts storage
        db_facts_mb = (number_of_hosts * self.FACTS_SIZE_PER_HOST_KB) / 1024

        # Inventory storage (similar to facts)
        db_inventory_mb = (number_of_hosts * self.FACTS_SIZE_PER_HOST_KB) / 1024

        # Jobs storage (MAIN COMPONENT)
        # Use verbosity level 1 (6 events/task) as baseline for database sizing
        baseline_events_per_task = 6  # Normal verbosity level
        db_jobs_mb = (
            number_of_hosts
            * jobs_per_host_per_day
            * tasks_per_job
            * baseline_events_per_task
            * days_to_keep_jobs
            * self.EVENT_SIZE_KB
        ) / 1024

        # Total database size
        db_total_gb = (db_facts_mb + db_inventory_mb + db_jobs_mb) / 1024

        return {
            "facts_mb": db_facts_mb,
            "inventory_mb": db_inventory_mb,
            "jobs_mb": db_jobs_mb,
            "total_gb": db_total_gb,
        }

    def calculate_execution_node_resources(self, current_metrics: dict[str, Any]) -> dict[str, Any]:
        """
        Calculate execution node resources using time-based concurrency formula.

        Uses Excel formulas:
        - Forks = hosts × jobs_per_hostday × job_duration / allowed_hours
        - Memory = forks × 100 / 1024 + 2 × nodes
        - CPU (AVG) = 2 × nodes + forks / 4 / 10
        """
        # Extract parameters
        managed_hosts = current_metrics.get("managed_hosts", 0)
        playbooks_per_day = current_metrics.get("playbooks_per_day_peak", 0)

        # Calculate jobs per host per day
        if managed_hosts > 0:
            jobs_per_host_per_day = playbooks_per_day / managed_hosts
        else:
            jobs_per_host_per_day = 0

        # Get job characteristics
        job_duration_hours = current_metrics.get("job_duration_hours", 0.25)  # 15 minutes default
        allowed_hours_per_day = current_metrics.get("allowed_hours_per_day", 24)  # 24/7 default
        peak_pattern = current_metrics.get(
            "peak_pattern", "distributed_24x7"
        )  # NEW: Peak concurrency pattern

        # Validate inputs and collect warnings
        self.warnings.extend(
            self.validate_input(
                "job_duration_hours", job_duration_hours, "Typical jobs: 5-60 minutes"
            )
        )
        self.warnings.extend(self.validate_input("managed_hosts", managed_hosts))
        self.warnings.extend(self.validate_input("playbooks_per_day_peak", playbooks_per_day))

        # Calculate forks needed with peak pattern multiplier
        forks_needed = self.calculate_execution_forks(
            managed_hosts,
            jobs_per_host_per_day,
            job_duration_hours,
            allowed_hours_per_day,
            peak_pattern,  # NEW: Apply peak pattern
        )

        # Start with minimum 2 nodes for HA
        execution_nodes = max(2, math.ceil(forks_needed / 50))  # ~50 forks per node as baseline

        # Calculate memory needed
        memory_total_gb = self.calculate_execution_memory(forks_needed, execution_nodes)

        # Calculate CPU needed (averaged)
        cpu_total = self.calculate_execution_cpu_avg(forks_needed, execution_nodes)

        # Adjust nodes if memory or CPU per node exceeds reasonable limits
        memory_per_pod = memory_total_gb / execution_nodes
        cpu_per_pod = cpu_total / execution_nodes

        # If memory per pod > 32GB or CPU > 8, add more nodes
        if memory_per_pod > 32:
            execution_nodes = math.ceil(memory_total_gb / 32)
            memory_per_pod = math.ceil(memory_total_gb / execution_nodes)
            cpu_per_pod = cpu_total / execution_nodes

        if cpu_per_pod > 8:
            execution_nodes = math.ceil(cpu_total / 8)
            cpu_per_pod = math.ceil(cpu_total / execution_nodes)
            memory_per_pod = math.ceil(memory_total_gb / execution_nodes)

        # Get peak pattern multiplier for display
        peak_multiplier = self.PEAK_CONCURRENCY_MULTIPLIERS.get(peak_pattern, 1.0)

        return {
            "execution_pods": execution_nodes,
            "cpu_per_pod": math.ceil(cpu_per_pod),
            "memory_per_pod_gb": math.ceil(memory_per_pod),
            "total_cpu": math.ceil(cpu_total),
            "total_memory_gb": math.ceil(memory_total_gb),
            "forks_needed": round(forks_needed, 2),
            "jobs_per_host_per_day": round(jobs_per_host_per_day, 2),
            "peak_pattern": peak_pattern,
            "peak_multiplier": peak_multiplier,
            "note": (
                f"Time-based calculation with peak pattern ({peak_pattern}, {peak_multiplier}x): "
                f"{managed_hosts} hosts × {round(jobs_per_host_per_day, 2)} jobs/host/day × "
                f"{job_duration_hours}h job / {allowed_hours_per_day}h day × {peak_multiplier} = "
                f"{round(forks_needed, 2)} forks"
            ),
        }

    def calculate_controller_resources(self, current_metrics: dict[str, Any]) -> dict[str, Any]:
        """
        Calculate automation controller control plane resources.

        Control plane needs BOTH event processing AND job management capacity,
        then uses the AVERAGE of both (as per Excel row 54).
        """
        # Extract parameters
        managed_hosts = current_metrics.get("managed_hosts", 0)
        playbooks_per_day = current_metrics.get("playbooks_per_day_peak", 0)
        tasks_per_job = current_metrics.get("tasks_per_job", 100)
        job_duration_hours = current_metrics.get("job_duration_hours", 0.25)
        allowed_hours_per_day = current_metrics.get("allowed_hours_per_day", 24)
        average_forks_per_job = current_metrics.get("forks_observed", 5)
        verbosity_level = current_metrics.get("verbosity_level", 1)  # NEW: Verbosity level (0-4)

        # Validate inputs (CRITICAL for control plane accuracy)
        self.warnings.extend(
            self.validate_input("tasks_per_job", tasks_per_job, "Typical playbooks: 10-100 tasks")
        )
        self.warnings.extend(
            self.validate_input("forks_observed", average_forks_per_job, "Typical forks: 5-25")
        )

        # Calculate jobs per host per day
        if managed_hosts > 0:
            jobs_per_host_per_day = playbooks_per_day / managed_hosts
        else:
            jobs_per_host_per_day = 0

        # Start with 2 control nodes for HA
        control_nodes = 2

        # Step 1: Calculate event processing capacity with verbosity level
        event_forks = self.calculate_event_forks(
            managed_hosts,
            jobs_per_host_per_day,
            tasks_per_job,
            job_duration_hours,
            allowed_hours_per_day,
            verbosity_level,  # NEW: Use verbosity level
        )

        # Get events per task for display
        events_per_task = self.VERBOSITY_EVENTS_PER_TASK.get(verbosity_level, 6)

        memory_for_events = self.calculate_control_memory_for_events(event_forks, control_nodes)
        cpu_for_events = self.calculate_control_cpu_for_events_avg(event_forks, control_nodes)

        # Step 2: Calculate job management capacity
        concurrent_jobs = (playbooks_per_day * job_duration_hours) / allowed_hours_per_day
        forks_for_jobs = concurrent_jobs * average_forks_per_job

        memory_for_jobs = self.calculate_control_memory_for_jobs(forks_for_jobs, control_nodes)
        cpu_for_jobs = self.calculate_control_cpu_for_jobs_avg(forks_for_jobs, control_nodes)

        # Step 3: AVERAGE both (as per Excel formula)
        memory_control_gb = (memory_for_events + memory_for_jobs) / 2
        cpu_control = (cpu_for_events + cpu_for_jobs) / 2

        # Adjust if per-node resources exceed limits
        memory_per_pod = memory_control_gb / control_nodes
        cpu_per_pod = cpu_control / control_nodes

        if memory_per_pod > 128:
            control_nodes = math.ceil(memory_control_gb / 128)
            memory_per_pod = math.ceil(memory_control_gb / control_nodes)
            cpu_per_pod = cpu_control / control_nodes

        if cpu_per_pod > 32:
            control_nodes = math.ceil(cpu_control / 32)
            cpu_per_pod = math.ceil(cpu_control / control_nodes)
            memory_per_pod = math.ceil(memory_control_gb / control_nodes)

        return {
            "control_plane_pods": control_nodes,
            "cpu_per_pod": math.ceil(cpu_per_pod),
            "memory_per_pod_gb": math.ceil(memory_per_pod),
            "total_cpu": math.ceil(cpu_control),
            "total_memory_gb": math.ceil(memory_control_gb),
            "event_forks": round(event_forks, 2),
            "forks_for_jobs": round(forks_for_jobs, 2),
            "verbosity_level": verbosity_level,
            "events_per_task": events_per_task,
            "calculation_breakdown": {
                "event_processing": {
                    "memory_gb": round(memory_for_events, 2),
                    "cpu": round(cpu_for_events, 2),
                },
                "job_management": {
                    "memory_gb": round(memory_for_jobs, 2),
                    "cpu": round(cpu_for_jobs, 2),
                },
                "averaged_result": {
                    "memory_gb": round(memory_control_gb, 2),
                    "cpu": round(cpu_control, 2),
                },
            },
            "note": f"Control plane uses AVERAGED result. Verbosity level {verbosity_level} generates {events_per_task} events/task",
        }

    def calculate_database_resources(self, current_metrics: dict[str, Any]) -> dict[str, Any]:
        """
        Calculate database resources based on workload.
        """
        # Extract parameters
        managed_hosts = current_metrics.get("managed_hosts", 0)
        playbooks_per_day = current_metrics.get("playbooks_per_day_peak", 0)
        tasks_per_job = current_metrics.get("tasks_per_job", 100)
        days_to_keep_jobs = current_metrics.get("job_retention_hours", 48) / 24

        # Calculate jobs per host per day
        if managed_hosts > 0:
            jobs_per_host_per_day = playbooks_per_day / managed_hosts
        else:
            jobs_per_host_per_day = 0

        # Calculate storage using Excel formula
        storage_breakdown = self.calculate_database_storage(
            managed_hosts, jobs_per_host_per_day, tasks_per_job, math.ceil(days_to_keep_jobs)
        )

        # Get current utilization if available
        cpu_percent = current_metrics.get("database_cpu_percent", 50)
        memory_percent = current_metrics.get("database_memory_percent", 35)
        current_db_vcpu = current_metrics.get("database_vcpu", 16)
        current_db_memory = current_metrics.get("database_memory_gb", 128)

        # Calculate actual used resources
        actual_cpu_used = current_db_vcpu * (cpu_percent / 100)
        actual_memory_used = current_db_memory * (memory_percent / 100)

        # Add headroom for growth and peaks
        recommended_cpu = max(8, math.ceil(actual_cpu_used * 1.3))  # 30% headroom
        recommended_memory = max(32, math.ceil(actual_memory_used * 1.5))  # 50% headroom

        # Storage with 20% buffer
        storage_with_buffer = math.ceil(storage_breakdown["total_gb"] * 1.2)

        return {
            "cpu": recommended_cpu,
            "memory_gb": recommended_memory,
            "storage_gb": max(60, storage_with_buffer),  # Minimum 60GB
            "iops": 3000,
            "storage_breakdown": {
                "facts_mb": round(storage_breakdown["facts_mb"], 2),
                "inventory_mb": round(storage_breakdown["inventory_mb"], 2),
                "jobs_mb": round(storage_breakdown["jobs_mb"], 2),
                "total_gb": round(storage_breakdown["total_gb"], 2),
            },
            "note": "Storage based on jobs history; CPU/Memory based on current utilization with headroom",
        }

    def calculate_automation_hub_resources(self, current_metrics: dict[str, Any]) -> dict[str, Any]:
        """
        Calculate automation hub resources.
        """
        cpu_percent = current_metrics.get("hub_cpu_percent", 25)
        memory_percent = current_metrics.get("hub_memory_percent", 30)
        num_hub_nodes = current_metrics.get("num_hub_nodes", 2)

        # Hub is less resource intensive, minimum 2 pods for HA
        hub_pods = max(2, num_hub_nodes)

        # Adjust based on utilization
        if cpu_percent > 50:
            cpu_per_pod = 4
        else:
            cpu_per_pod = 2

        if memory_percent > 50:
            memory_per_pod = 16
        else:
            memory_per_pod = 8

        return {
            "hub_pods": hub_pods,
            "cpu_per_pod": cpu_per_pod,
            "memory_per_pod_gb": memory_per_pod,
            "total_cpu": hub_pods * cpu_per_pod,
            "total_memory_gb": hub_pods * memory_per_pod,
            "note": "Minimum 2 pods for HA; scale based on collection sync and content serving needs",
        }

    def calculate_gateway_resources(self, current_metrics: dict[str, Any]) -> dict[str, Any]:
        """
        Calculate platform gateway resources.
        """
        managed_hosts = current_metrics.get("managed_hosts", 0)

        if managed_hosts > 20000:
            gateway_pods = 3  # HA configuration
            cpu_per_pod = 2
            memory_per_pod = 4
        else:
            gateway_pods = 2  # Basic HA
            cpu_per_pod = 2
            memory_per_pod = 4

        return {
            "gateway_pods": gateway_pods,
            "cpu_per_pod": cpu_per_pod,
            "memory_per_pod_gb": memory_per_pod,
            "total_cpu": gateway_pods * cpu_per_pod,
            "total_memory_gb": gateway_pods * memory_per_pod,
            "note": "Gateway handles authentication and routing; 2-3 pods for HA",
        }

    def calculate_eda_resources(self, current_metrics: dict[str, Any]) -> dict[str, Any]:
        """
        Calculate Event-Driven Ansible resources.
        """
        # Basic EDA setup, can be scaled based on activations
        eda_pods = 2
        cpu_per_pod = 2
        memory_per_pod = 8

        return {
            "eda_pods": eda_pods,
            "cpu_per_pod": cpu_per_pod,
            "memory_per_pod_gb": memory_per_pod,
            "total_cpu": eda_pods * cpu_per_pod,
            "total_memory_gb": eda_pods * memory_per_pod,
            "note": "Scale based on number of activations and event rates",
        }

    def calculate_redis_resources(self, current_metrics: dict[str, Any]) -> dict[str, Any]:
        """
        Calculate Redis resources.
        """
        managed_hosts = current_metrics.get("managed_hosts", 0)

        if managed_hosts > 10000:
            # Clustered Redis for enterprise
            return {
                "type": "clustered",
                "primary_nodes": 3,
                "replica_nodes": 3,
                "cpu_per_node": 1,
                "memory_per_node_gb": 4,
                "total_nodes": 6,
                "total_cpu": 6,
                "total_memory_gb": 24,
                "note": "Clustered Redis (3 primary + 3 replica) for high availability",
            }
        else:
            # Standalone Redis
            return {
                "type": "standalone",
                "nodes": 1,
                "cpu": 1,
                "memory_gb": 2,
                "total_cpu": 1,
                "total_memory_gb": 2,
                "note": "Standalone Redis for smaller deployments",
            }

    # AAP 2.6 Tested Topology Specifications (from Red Hat documentation)
    TOPOLOGY_SPECS: dict[str, dict[str, Any]] = {
        "ocp": {
            "growth": {
                "description": "Single Node OpenShift (SNO) - all components on one node",
                "node_spec": {"cpu": 16, "memory_gb": 32, "disk_gb": 128, "iops": 3000},
                "nodes": 1,
                "db": "operator-managed pod (100 max_connections, 100 GB limit)",
                "redis": "operator-managed pod",
                "hub_storage": "S3 (ReadWriteMany required)",
                "limitations": [
                    "No redundancy - single point of failure",
                    "Operator-deployed DB limited to 100 max_connections and 100 GB",
                    "Must use external DB if: >1 replica of any component, >100 concurrent jobs, or >100 GB storage",
                    "Not suitable for production workloads requiring HA",
                ],
                "doc_link": "https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.6/html/planning_your_installation/ocp-topologies",
            },
            "enterprise": {
                "description": "Multi-worker OpenShift cluster with external services",
                "node_spec": {"cpu": 4, "memory_gb": 16, "disk_gb": 128, "iops": 3000},
                "min_workers": 2,
                "external_db_spec": {
                    "cpu": 4,
                    "memory_gb": 16,
                    "storage_gb": 200,
                    "iops": 3000,
                    "max_connections": 1024,
                },
                "redis": "operator-managed Redis (do not use external for AAP 2.6)",
                "hub_storage": "S3 (ReadWriteMany required)",
                "doc_link": "https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.6/html/planning_your_installation/ocp-topologies",
            },
        },
        "containerized": {
            "growth": {
                "description": "Single RHEL VM with Podman - all components colocated",
                "vm_spec": {"cpu": 4, "memory_gb": 16, "disk_gb": 60, "iops": 3000},
                "vm_count": 1,
                "redis": "standalone, colocated",
                "limitations": [
                    "No redundancy - single point of failure",
                    "All components share a single VM's resources",
                    "Not suitable for production workloads requiring HA",
                    "External Redis not supported for containerized installs",
                ],
                "doc_link": "https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.6/html/planning_your_installation/container-topologies",
            },
            "enterprise": {
                "description": "Multi-VM RHEL deployment with Podman and HA",
                "vm_spec": {"cpu": 4, "memory_gb": 16, "disk_gb": 60, "iops": 3000},
                "vm_layout": [
                    {"purpose": "Platform gateway + colocated Redis", "count": 2},
                    {"purpose": "Automation controller", "count": 2},
                    {"purpose": "Automation hub + colocated Redis", "count": 2},
                    {"purpose": "Event-Driven Ansible + colocated Redis", "count": 2},
                    {"purpose": "Automation mesh execution node", "count": 2},
                    {"purpose": "External database (managed separately)", "count": 1},
                ],
                "redis": "HA mode, colocated on component VMs (6 VMs min for Redis HA). External Redis not supported.",
                "doc_link": "https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.6/html/planning_your_installation/container-topologies",
            },
        },
    }

    def _recommend_topology(
        self,
        deployment_target: str,
        execution: dict[str, Any],
        controller: dict[str, Any],
        database: dict[str, Any],
        current_metrics: dict[str, Any],
    ) -> dict[str, Any]:
        """Recommend growth vs enterprise topology based on computed resources."""
        concurrent_jobs = execution.get("forks_needed", 0) / max(
            current_metrics.get("forks_observed", 5), 1
        )
        db_storage = database.get("storage_gb", 0)
        needs_multiple_controllers = controller.get("control_plane_pods", 2) > 1
        needs_multiple_execution = execution.get("execution_pods", 2) > 2
        num_current_controllers = current_metrics.get("num_controllers", 1)
        managed_hosts = current_metrics.get("managed_hosts", 0)

        enterprise_reasons: list[str] = []

        if concurrent_jobs > 100:
            enterprise_reasons.append(
                f"Estimated {int(concurrent_jobs)} concurrent jobs exceeds growth DB limit of 100"
            )
        if db_storage > 100 and deployment_target == "ocp":
            enterprise_reasons.append(
                f"Estimated {db_storage} GB database exceeds operator-managed DB limit of 100 GB"
            )
        if needs_multiple_controllers and deployment_target == "ocp":
            enterprise_reasons.append(
                "Multiple controller replicas needed (growth supports only 1 replica per component)"
            )
        if needs_multiple_execution:
            unit = "instances" if deployment_target == "containerized" else "pods"
            enterprise_reasons.append(
                f"{execution.get('execution_pods', 0)} execution {unit} needed (growth has limited capacity)"
            )
        if num_current_controllers > 2:
            enterprise_reasons.append(
                f"Current environment has {num_current_controllers} controllers indicating enterprise workload"
            )
        if managed_hosts > 5000:
            enterprise_reasons.append(
                f"{managed_hosts} managed hosts exceeds typical growth topology capacity"
            )

        growth_viable = len(enterprise_reasons) == 0
        recommended = "growth" if growth_viable else "enterprise"

        specs = self.TOPOLOGY_SPECS[deployment_target]
        topology_info = specs[recommended]

        result: dict[str, Any] = {
            "target": deployment_target,
            "recommended_topology": recommended,
            "growth_viable": growth_viable,
            "doc_link": topology_info["doc_link"],
        }

        if recommended == "enterprise":
            result["enterprise_reasons"] = enterprise_reasons
        else:
            result["growth_limitations"] = topology_info.get("limitations", [])

        if deployment_target == "ocp":
            result.update(self._ocp_deployment_details(recommended, execution, database, specs))
        else:
            result.update(
                self._containerized_deployment_details(recommended, execution, database, specs)
            )

        return result

    def _ocp_deployment_details(
        self,
        topology: str,
        execution: dict[str, Any],
        database: dict[str, Any],
        specs: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate OCP-specific deployment details."""
        details: dict[str, Any] = {}

        if topology == "growth":
            details["cluster_type"] = "Single Node OpenShift (SNO)"
            details["node_spec"] = specs["growth"]["node_spec"]
            details["total_nodes"] = 1
            details["db_type"] = specs["growth"]["db"]
            details["redis"] = "Operator-managed Redis pod (in-cluster)"
            details["hub_storage"] = specs["growth"]["hub_storage"]
        else:
            worker_spec = specs["enterprise"]["node_spec"]
            total_cpu = execution.get("total_cpu", 8) + 16  # execution + other pods
            total_memory = execution.get("total_memory_gb", 16) + 32
            workers_by_cpu = math.ceil(total_cpu / worker_spec["cpu"])
            workers_by_mem = math.ceil(total_memory / worker_spec["memory_gb"])
            worker_count = max(specs["enterprise"]["min_workers"], workers_by_cpu, workers_by_mem)

            details["cluster_type"] = "Multi-worker OpenShift cluster"
            details["worker_nodes"] = worker_count
            details["worker_spec"] = worker_spec
            details["external_db"] = specs["enterprise"]["external_db_spec"]
            details["external_db"]["recommended_storage_gb"] = max(
                200, database.get("storage_gb", 60)
            )
            details["redis"] = "Operator-managed Redis (do not use external — changes in AAP 2.7)"
            details["hub_storage"] = specs["enterprise"]["hub_storage"]

        return details

    def _containerized_deployment_details(
        self,
        topology: str,
        execution: dict[str, Any],
        database: dict[str, Any],
        specs: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate containerized (Podman) deployment details."""
        details: dict[str, Any] = {}

        if topology == "growth":
            details["vm_count"] = 1
            details["vm_spec"] = specs["growth"]["vm_spec"]
            details["redis"] = "Standalone mode, colocated (external Redis not supported)"
            details["layout"] = (
                "All components on single VM: gateway, controller, hub, EDA, database, Redis"
            )
        else:
            vm_spec = specs["enterprise"]["vm_spec"]
            layout = specs["enterprise"]["vm_layout"]
            # Adjust execution node count based on calculated needs
            exec_pods = execution.get("execution_pods", 2)
            adjusted_layout = []
            for item in layout:
                if "execution node" in item["purpose"].lower():
                    adjusted_layout.append(
                        {"purpose": item["purpose"], "count": max(item["count"], exec_pods)}
                    )
                else:
                    adjusted_layout.append(item)

            total_vms = sum(item["count"] for item in adjusted_layout)
            details["vm_count"] = total_vms
            details["vm_spec"] = vm_spec
            details["vm_layout"] = adjusted_layout
            details["redis"] = "HA mode colocated on component VMs (external Redis not supported)"
            details["db_storage_recommended_gb"] = max(60, database.get("storage_gb", 60))

        return details

    def generate_sizing_recommendation(
        self, current_metrics: dict[str, Any], deployment_target: str = "ocp"
    ) -> dict[str, Any]:
        """
        Generate complete sizing recommendation for AAP 2.6 based on AAP 2.4 metrics.
        Uses official Red Hat Excel reference formulas with enhanced accuracy validation.

        Args:
            current_metrics: Workload metrics dict
            deployment_target: "ocp" or "containerized"
        """
        # Clear any previous warnings
        self.warnings = []

        # Calculate resources for each component
        gateway = self.calculate_gateway_resources(current_metrics)
        controller = self.calculate_controller_resources(current_metrics)
        execution = self.calculate_execution_node_resources(current_metrics)
        database = self.calculate_database_resources(current_metrics)
        hub = self.calculate_automation_hub_resources(current_metrics)
        eda = self.calculate_eda_resources(current_metrics)
        redis = self.calculate_redis_resources(current_metrics)

        # For containerized, Redis is always colocated on component VMs (not standalone)
        if deployment_target == "containerized":
            redis = {
                "type": "colocated_ha",
                "total_cpu": 0,
                "total_memory_gb": 0,
                "note": "Redis runs colocated on gateway, hub, and EDA VMs (external Redis not supported)",
            }
            # Each VM in containerized is 4 CPU / 16 GB — enforce per-instance minimums
            for comp in [gateway, controller, execution, hub, eda]:
                if comp.get("cpu_per_pod", 0) < 4:
                    comp["cpu_per_pod"] = 4
                if comp.get("memory_per_pod_gb", 0) < 16:
                    comp["memory_per_pod_gb"] = 16
                # Recompute totals
                pod_key = next((k for k in comp if k.endswith("_pods")), None)
                if pod_key:
                    comp["total_cpu"] = comp[pod_key] * comp["cpu_per_pod"]
                    comp["total_memory_gb"] = comp[pod_key] * comp["memory_per_pod_gb"]

        # Calculate totals
        total_cpu = (
            gateway["total_cpu"]
            + controller["total_cpu"]
            + execution["total_cpu"]
            + database["cpu"]
            + hub["total_cpu"]
            + eda["total_cpu"]
            + redis["total_cpu"]
        )

        total_memory = (
            gateway["total_memory_gb"]
            + controller["total_memory_gb"]
            + execution["total_memory_gb"]
            + database["memory_gb"]
            + hub["total_memory_gb"]
            + eda["total_memory_gb"]
            + redis["total_memory_gb"]
        )

        # Validate results and add warnings
        results_warnings = self.validate_results(execution, controller, database)
        all_warnings = self.warnings + results_warnings

        # Recommend topology based on deployment target and computed resources
        deployment = self._recommend_topology(
            deployment_target, execution, controller, database, current_metrics
        )

        return {
            "deployment": deployment,
            "components": {
                "platform_gateway": gateway,
                "automation_controller_control_plane": controller,
                "automation_controller_execution_plane": execution,
                "database": database,
                "automation_hub": hub,
                "event_driven_ansible": eda,
                "redis": redis,
            },
            "summary": {
                "total_cpu": total_cpu,
                "total_memory_gb": total_memory,
                "total_storage_gb": database["storage_gb"],
                "estimated_pods": (
                    gateway["gateway_pods"]
                    + controller["control_plane_pods"]
                    + execution["execution_pods"]
                    + hub["hub_pods"]
                    + eda["eda_pods"]
                    + redis.get("total_nodes", redis.get("nodes", 0))
                ),
            },
            "formulas_used": {
                "source": "Red Hat AAP Excel Reference Sheet (AAp-sizing-sheet-reference.xlsx)",
                "execution_forks": "hosts × jobs_per_host_per_day × job_duration_hours / allowed_hours_per_day × peak_multiplier",
                "execution_memory": "forks × 100MB / 1024 + 2GB × nodes",
                "execution_cpu": "2 × nodes + forks / 4 / 10 (averaged)",
                "control_plane": "AVERAGE of (event_processing + job_management) / 2",
                "event_forks": "hosts × jobs_per_host_per_day × tasks_per_job × events/task (verbosity-based) × duration / allowed_hours",
                "database_storage": "hosts × jobs_per_host_per_day × tasks_per_job × events/task × retention_days × 2KB / 1024",
            },
            "warnings": all_warnings,
            "deployment_notes": self._get_deployment_notes(
                current_metrics, execution, controller, deployment_target
            ),
        }

    def _get_deployment_notes(
        self,
        metrics: dict[str, Any],
        execution: dict[str, Any],
        controller: dict[str, Any],
        deployment_target: str = "ocp",
    ) -> list:
        """Generate deployment notes and recommendations."""
        notes = []

        notes.append("✓ Calculations based on official Red Hat Excel reference formulas")
        notes.append(
            "✓ Uses time-based concurrency: forks = hosts × jobs/host/day × duration / allowed_hours"
        )
        notes.append("✓ Control plane uses AVERAGED result of event processing AND job management")
        notes.append(f"✓ Execution plane: {execution.get('forks_needed', 0)} forks needed")
        notes.append(
            f"✓ Control plane: {controller.get('event_forks', 0)} event forks, "
            f"{controller.get('forks_for_jobs', 0)} job forks"
        )

        notes.append("All values include appropriate headroom for peaks and growth")

        if deployment_target == "ocp":
            notes.append(
                "OCP: Redis is operator-managed (do not use external Redis — this changes in AAP 2.7)"
            )
            notes.append("OCP: Automation hub requires S3 or ReadWriteMany storage")
            notes.append(
                "OCP: Operator-deployed DB limited to 100 connections / 100 GB — use external DB for enterprise"
            )
        else:
            notes.append(
                "Containerized: External Redis is NOT supported — Redis must be colocated on component VMs"
            )
            notes.append("Containerized: Enterprise topology requires 6+ VMs for Redis HA")
            notes.append(
                "Containerized: Each VM minimum 4 CPU / 16 GB RAM / 60 GB disk / 3000 IOPS"
            )

        managed_hosts = metrics.get("managed_hosts", 0)
        if managed_hosts > 20000:
            notes.append("Consider separate PostgreSQL instances per component for isolation")
            notes.append("Implement load balancing for platform gateway endpoints")

        notes.append("Test in non-production environment before migration")
        notes.append("Monitor and adjust resources post-migration based on actual usage")
        notes.append("Validate sizing with Red Hat support for production deployments")

        return notes


def main() -> None:
    """Example usage"""
    calculator = AAP26SizingCalculator()

    # Example: User's current AAP 2.4 environment
    current_aap24_metrics = {
        # Managed environment
        "managed_hosts": 40000,
        # Workload characteristics
        "playbooks_per_day_peak": 70000,
        "tasks_per_job": 100,  # Average tasks per playbook
        "job_duration_hours": 0.25,  # 15 minutes average
        "allowed_hours_per_day": 24,  # 24/7 operation
        "job_retention_hours": 48,  # Keep jobs for 2 days
        "forks_observed": 5,  # Average forks per job
        # Current controllers
        "num_controllers": 12,
        # Automation Hub
        "num_hub_nodes": 2,
        "hub_cpu_percent": 25,
        "hub_memory_percent": 30,
        # Database
        "database_vcpu": 16,
        "database_memory_gb": 128,
        "database_cpu_percent": 90,
        "database_memory_percent": 35,
    }

    recommendation = calculator.generate_sizing_recommendation(current_aap24_metrics)

    import json

    print(json.dumps(recommendation, indent=2))


if __name__ == "__main__":
    main()
