# AAP Bridge TUI Screenshots

## Main Menu

```
┌──────────────────────────────────────────────────────────┐
│          AAP Migration Tool - Main Menu                  │
└──────────────────────────────────────────────────────────┘

1. Export Resources
2. Transform Resources
3. Import Resources
4. Credential Operations
5. Validation & Reporting

b. Back / Exit

Select an option:
```

```
<img width="675" height="291" alt="Screenshot 2026-04-01 at 17 55 18" src="https://github.com/user-attachments/assets/75a6a9ea-75ab-4294-9237-9e1d81d8cf07" />
```

## Import Resources Menu

```
┌──────────────────────────────────────────────────────────┐
│                    Import Resources                       │
└──────────────────────────────────────────────────────────┘

1. Pre-flight Check (Validate Dependencies)
2. Import All Resources (Automatic)
3. Granular Import (Step-by-Step Control) ⭐ Recommended
4. View Import Status

b. Back to Main Menu

Select an option:
```

## Granular Import - Phase Progress

```
┌──────────────────────────────────────────────────────────┐
│           Granular Micro-Phase Import                     │
└──────────────────────────────────────────────────────────┘

Import resources step-by-step with full control.
You can skip phases, retry, or abort at any time.

Phase  Resource Type          Total  ✓    ✗    ⧗    Progress           Status
─────────────────────────────────────────────────────────────────────────────
1.1    Organizations           15    15   -    -    ███████████████ 100% ✓ Done
1.2    Users                   32    32   -    -    ███████████████ 100% ✓ Done
1.3    Teams                   21    21   -    -    ███████████████ 100% ✓ Done
2.1    Credential Types        35    35   -    -    ███████████████ 100% ✓ Done
2.2    Credentials             57    57   -    -    ███████████████ 100% ✓ Done
3.1    Execution Environments  15    15   -    -    ███████████████ 100% ✓ Done
3.2    Projects                13    13   -    -    ███████████████ 100% ✓ Done
       Patching Projects       13    13   -    -    ███████████████ 100% ✓ Done
3.3    Inventories             13    13   -    -    ███████████████ 100% ✓ Done
3.4    Inventory Sources        2     2   -    -    ███████████████ 100% ✓ Done
3.5    Inventory Groups        15     -   -    15   ░░░░░░░░░░░░░░░   0% ⧗ Pending
4.1    Hosts                   22     -   -    22   ░░░░░░░░░░░░░░░   0% ⧗ Pending

Press Enter to start...
```

## Import Status View

```
┌──────────────────────────────────────────────────────────┐
│                     Import Status                         │
└──────────────────────────────────────────────────────────┘

Resource Type          Total  Completed  Failed  Pending  Progress
────────────────────────────────────────────────────────────────────
organizations            15      15        -       -      ███████████████ 100.0%
users                    32      32        -       -      ███████████████ 100.0%
teams                    21      21        -       -      ███████████████ 100.0%
credential_types         35      35        -       -      ███████████████ 100.0%
credentials              57      57        -       -      ███████████████ 100.0%
execution_environments   15      15        -       -      ███████████████ 100.0%
projects                 13      13        -       -      ███████████████ 100.0%
inventories              13      13        -       -      ███████████████ 100.0%
inventory_sources         2       2        -       -      ███████████████ 100.0%
inventory_groups         15      15        -       -      ███████████████ 100.0%
hosts                    22      22        -       -      ███████████████ 100.0%
job_templates            18      18        -       -      ███████████████ 100.0%
workflow_job_templates    6       6        -       -      ███████████████ 100.0%
schedules                11      11        -       -      ███████████████ 100.0%
applications              3       3        -       -      ███████████████ 100.0%

Overall Progress:
  Total Resources: 278
  ✓ Completed: 278
  ✗ Failed: 0
  ⧗ Pending: 0

⚠️  Important Note:
Check inventory sources manually for outdated EE's which are pointing to
older AAP-2.4 automation hub address.

Press Enter to continue...
```

## Key Features Shown in TUI

1. **Visual Progress Bars**: Clear indication of import progress for each phase
2. **Color-Coded Status**:
   - Green (✓) = Completed
   - Red (✗) = Failed
   - Yellow (⧗) = Pending/In Progress
3. **Phase-by-Phase Control**: Import one micro-phase at a time
4. **Real-Time Statistics**: See counts for total, completed, failed, pending
5. **User Warnings**: Important notes about EE configuration and manual checks
6. **Interactive Menus**: Easy navigation with numbered options
7. **Breadcrumbs**: Always know where you are in the workflow

## TUI Advantages

✅ **No complex commands to remember** - Just select numbered options
✅ **Visual feedback** - See exactly what's happening at each step
✅ **Error visibility** - Failed resources are clearly marked
✅ **Pause and resume** - Full control over the migration process
✅ **Built-in help** - Contextual information at every step
✅ **Safe workflow** - Confirmation prompts before critical operations
