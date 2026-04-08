"""
Analyze merge migrations for duplicate state-altering operations across branches.
"""
import ast
import os
import sys
from collections import defaultdict

APPS_DIR = os.path.join(os.path.dirname(__file__), 'apps')

MERGE_MIGRATIONS = {
    'network': {
        'merge_file': '0013_merge_20260408_1910',
        'branch_a_tip': '0004_alter_router_api_password_and_more',
        'branch_b_tip': '0012_alter_ipaddress_ip_pool_alter_router_api_password_and_more',
    },
    'billing': {
        'merge_file': '0016_merge_20260408_1910',
        'branch_a_tip': '0008_hotspotclient_hotspotclientdevice_mpesaconfiguration_and_more',
        'branch_b_tip': '0015_hotspotclient_canonical_username_and_more',
    },
    'core': {
        'merge_file': '0008_merge_20260408_1910',
        'branch_a_tip': '0003_changelog_tumacallbackmap_featurerequest_and_more',
        'branch_b_tip': '0007_tumacallbackmap',
    },
    'radius': {
        'merge_file': '0007_merge_20260408_1910',
        'branch_a_tip': '0003_customerradiuscredentials_subscription_activated_at',
        'branch_b_tip': '0006_remove_radiustenantconfig_container_name_and_more',
    },
    'customers': {
        'merge_file': '0004_merge_20260408_1910',
        'branch_a_tip': '0002_alter_customer_id_number',
        'branch_b_tip': '0003_serviceconnection_billing_account_number_and_more',
    },
    'subscriptions': {
        'merge_file': '0006_merge_20260408_1910',
        'branch_a_tip': '0003_netilyplan_base_license_fee_and_more',
        'branch_b_tip': '0005_remove_billingcycle_snapshot_min_clients_and_more',
    },
}


def parse_migration_file(filepath):
    """Parse a migration file and return dependencies and operations."""
    with open(filepath, 'r', encoding='utf-8') as f:
        source = f.read()
    
    tree = ast.parse(source)
    
    deps = []
    ops = []
    
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == 'Migration':
            for item in node.body:
                if isinstance(item, ast.Assign):
                    for target in item.targets:
                        if isinstance(target, ast.Name) and target.id == 'dependencies':
                            deps = extract_dependencies(item.value)
                        elif isinstance(target, ast.Name) and target.id == 'operations':
                            ops = extract_operations(item.value)
    
    return deps, ops


def extract_dependencies(node):
    """Extract dependency tuples from the AST."""
    deps = []
    if isinstance(node, ast.List):
        for elt in node.elts:
            if isinstance(elt, ast.Tuple) and len(elt.elts) == 2:
                app = get_string_value(elt.elts[0])
                mig = get_string_value(elt.elts[1])
                if app and mig:
                    deps.append((app, mig))
    return deps


def get_string_value(node):
    """Get string value from AST node."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def extract_operations(node):
    """Extract operations from the operations list."""
    ops = []
    if isinstance(node, ast.List):
        for elt in node.elts:
            op = extract_single_operation(elt)
            if op:
                ops.append(op)
    return ops


def extract_single_operation(node):
    """Extract a single migration operation."""
    if not isinstance(node, ast.Call):
        return None
    
    # Get the operation type (e.g., migrations.AddField)
    if isinstance(node.func, ast.Attribute):
        op_type = node.func.attr
    else:
        return None
    
    # Extract keyword arguments
    kwargs = {}
    for kw in node.keywords:
        if kw.arg and isinstance(kw.arg, str):
            val = get_string_value(kw.value)
            if val:
                kwargs[kw.arg] = val
    
    # Also check positional args for CreateModel (first arg is name)
    if op_type == 'CreateModel' and node.args:
        name = get_string_value(node.args[0])
        if name:
            kwargs['name'] = name
    
    return {
        'type': op_type,
        'model_name': kwargs.get('model_name', kwargs.get('name', '')),
        'name': kwargs.get('name', ''),
        'field_name': kwargs.get('name', ''),
    }


def get_migration_files(app_name):
    """Get all migration files for an app, keyed by migration name."""
    migrations_dir = os.path.join(APPS_DIR, app_name, 'migrations')
    files = {}
    for f in os.listdir(migrations_dir):
        if f.endswith('.py') and f != '__init__.py':
            name = f[:-3]  # strip .py
            files[name] = os.path.join(migrations_dir, f)
    return files


def build_dependency_graph(app_name, migration_files):
    """Build a dependency graph: migration_name -> list of (app, dep_name)."""
    graph = {}
    for name, filepath in migration_files.items():
        try:
            deps, ops = parse_migration_file(filepath)
            graph[name] = {
                'deps': deps,
                'ops': ops,
                'filepath': filepath,
            }
        except Exception as e:
            print(f"  WARNING: Could not parse {filepath}: {e}")
            graph[name] = {'deps': [], 'ops': [], 'filepath': filepath}
    return graph


def trace_branch(graph, app_name, tip_name, common_ancestor):
    """
    Trace a branch backwards from tip to common ancestor (exclusive).
    Returns list of migration names in the branch (excluding common ancestor).
    """
    branch = []
    visited = set()
    stack = [tip_name]
    
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        
        if current == common_ancestor:
            continue  # Don't include the common ancestor
        
        if current not in graph:
            continue
            
        branch.append(current)
        
        for dep_app, dep_name in graph[current]['deps']:
            if dep_app == app_name and dep_name not in visited:
                stack.append(dep_name)
    
    return branch


def find_common_ancestor(graph, app_name, tip_a, tip_b):
    """Find the common ancestor of two branch tips."""
    # Collect all ancestors of tip_a
    ancestors_a = set()
    stack = [tip_a]
    while stack:
        current = stack.pop()
        if current in ancestors_a:
            continue
        ancestors_a.add(current)
        if current in graph:
            for dep_app, dep_name in graph[current]['deps']:
                if dep_app == app_name:
                    stack.append(dep_name)
    
    # Find first ancestor of tip_b that's also in ancestors_a
    stack = [tip_b]
    visited = set()
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        if current in ancestors_a and current != tip_a and current != tip_b:
            return current
        if current in graph:
            for dep_app, dep_name in graph[current]['deps']:
                if dep_app == app_name:
                    stack.append(dep_name)
    
    return '0001_initial'  # fallback


def collect_ops_for_branch(graph, branch_migrations):
    """Collect all state-altering operations for a branch."""
    ops = []
    for mig_name in branch_migrations:
        if mig_name in graph:
            for op in graph[mig_name]['ops']:
                op_with_source = dict(op)
                op_with_source['source_migration'] = mig_name
                ops.append(op_with_source)
    return ops


def normalize_op_key(op):
    """Create a normalized key for comparing operations."""
    op_type = op['type']
    model = op['model_name'].lower()
    
    if op_type in ('AddField', 'RemoveField'):
        field = op['field_name'].lower()
        return (op_type, model, field)
    elif op_type == 'CreateModel':
        return (op_type, model, '')
    elif op_type == 'DeleteModel':
        return (op_type, model, '')
    elif op_type == 'AlterField':
        field = op['field_name'].lower()
        return (op_type, model, field)
    elif op_type == 'AddIndex':
        return (op_type, model, op.get('name', '').lower())
    elif op_type == 'RemoveIndex':
        return (op_type, model, op.get('name', '').lower())
    elif op_type in ('AlterUniqueTogether', 'AlterIndexTogether'):
        return (op_type, model, '')
    else:
        return (op_type, model, op.get('name', '').lower())


def analyze_app(app_name, config):
    """Analyze a single app for duplicate operations across merge branches."""
    print(f"\n{'='*80}")
    print(f"APP: {app_name}")
    print(f"{'='*80}")
    
    migration_files = get_migration_files(app_name)
    graph = build_dependency_graph(app_name, migration_files)
    
    tip_a = config['branch_a_tip']
    tip_b = config['branch_b_tip']
    
    # Find common ancestor
    common = find_common_ancestor(graph, app_name, tip_a, tip_b)
    print(f"  Common ancestor: {common}")
    print(f"  Branch A tip: {tip_a}")
    print(f"  Branch B tip: {tip_b}")
    
    # Trace branches
    branch_a = trace_branch(graph, app_name, tip_a, common)
    branch_b = trace_branch(graph, app_name, tip_b, common)
    
    print(f"\n  Branch A migrations ({len(branch_a)}):")
    for m in sorted(branch_a):
        print(f"    - {m}")
    
    print(f"\n  Branch B migrations ({len(branch_b)}):")
    for m in sorted(branch_b):
        print(f"    - {m}")
    
    # Collect operations
    ops_a = collect_ops_for_branch(graph, branch_a)
    ops_b = collect_ops_for_branch(graph, branch_b)
    
    # Print all operations
    print(f"\n  Branch A operations ({len(ops_a)}):")
    for op in ops_a:
        if op['type'] in ('AddField', 'RemoveField', 'CreateModel', 'DeleteModel', 'AlterField'):
            field_info = f".{op['field_name']}" if op['field_name'] and op['type'] != 'CreateModel' else ''
            print(f"    {op['type']}({op['model_name']}{field_info}) in {op['source_migration']}")
    
    print(f"\n  Branch B operations ({len(ops_b)}):")
    for op in ops_b:
        if op['type'] in ('AddField', 'RemoveField', 'CreateModel', 'DeleteModel', 'AlterField'):
            field_info = f".{op['field_name']}" if op['field_name'] and op['type'] != 'CreateModel' else ''
            print(f"    {op['type']}({op['model_name']}{field_info}) in {op['source_migration']}")
    
    # Find duplicates
    ops_a_keyed = defaultdict(list)
    ops_b_keyed = defaultdict(list)
    
    for op in ops_a:
        key = normalize_op_key(op)
        ops_a_keyed[key].append(op)
    
    for op in ops_b:
        key = normalize_op_key(op)
        ops_b_keyed[key].append(op)
    
    # Find intersections
    duplicates = []
    for key in ops_a_keyed:
        if key in ops_b_keyed:
            for op_a in ops_a_keyed[key]:
                for op_b in ops_b_keyed[key]:
                    duplicates.append({
                        'op_type': key[0],
                        'model': key[1],
                        'field': key[2],
                        'branch_a_migration': op_a['source_migration'],
                        'branch_b_migration': op_b['source_migration'],
                    })
    
    if duplicates:
        print(f"\n  *** DUPLICATE OPERATIONS FOUND: {len(duplicates)} ***")
        for d in duplicates:
            field_str = f".{d['field']}" if d['field'] else ''
            print(f"    CONFLICT: {d['op_type']}({d['model']}{field_str})")
            print(f"      Branch A: {d['branch_a_migration']}")
            print(f"      Branch B: {d['branch_b_migration']}")
    else:
        print(f"\n  No duplicate operations found.")
    
    return duplicates


def main():
    all_conflicts = []
    
    for app_name, config in MERGE_MIGRATIONS.items():
        try:
            conflicts = analyze_app(app_name, config)
            for c in conflicts:
                c['app'] = app_name
            all_conflicts.extend(conflicts)
        except Exception as e:
            print(f"\n  ERROR analyzing {app_name}: {e}")
            import traceback
            traceback.print_exc()
    
    # Print summary table
    print(f"\n\n{'='*120}")
    print("COMPLETE CONFLICT SUMMARY TABLE")
    print(f"{'='*120}")
    print(f"{'App':<15} {'Operation':<20} {'Model':<35} {'Field':<30} {'Branch A Migration':<50} {'Branch B Migration'}")
    print(f"{'-'*15} {'-'*20} {'-'*35} {'-'*30} {'-'*50} {'-'*50}")
    
    for c in all_conflicts:
        print(f"{c['app']:<15} {c['op_type']:<20} {c['model']:<35} {c['field']:<30} {c['branch_a_migration']:<50} {c['branch_b_migration']}")
    
    if not all_conflicts:
        print("  No conflicts found across any app.")
    
    print(f"\nTotal conflicts: {len(all_conflicts)}")


if __name__ == '__main__':
    main()
