# Godot support in Graphify for Godot

This page describes the current static support in the community fork. It is
for Godot 4 text projects. Graphify reads project files without launching Godot
or running the game. “Implemented” means covered by fixtures and connected to
the final graph; it does not mean Godot runtime behavior has been proven.

For planned completion gates, see the Tiny Demons
[GDScript completion plan](../../docs/graphify-gdscript-completion-plan.md).

## Support matrix

| Surface | Status | Evidence | Limits |
| --- | --- | --- | --- |
| Discovery and extraction | Implemented | `tests/test_gdscript.py`, `tests/test_godot_text.py` | GDScript parsing requires the optional `graphifyy[godot]` extra. Missing grammar does not provide GDScript semantic extraction. |
| Script structure | Partial | `test_script_class_methods_and_exported_properties_are_extracted`, `test_signals_enums_constants_and_nested_members_are_owned`, `test_inline_compound_statements_parse_with_original_locations`, `test_lambda_calls_and_signal_callbacks_have_lambda_owners` | Covers script classes, methods, top-level properties, signals, enums, constants, nested classes, static methods, annotations, parameter/return text, inline accessor bodies, and explicit lambda ownership. Source-preserving normalization handles compact one-line compound statements and semicolon sequences. Other grammar gaps and precise recovery spans remain open. |
| Literal resource references | Implemented | `test_literal_resource_and_global_class_references_are_extracted`, `test_gdscript_smoke_dependencies_resolve_to_resources_and_global_classes` | Resolves literal `preload`, `load`, and `ResourceLoader.load` paths in the indexed project. Computed paths are reported as dynamic. Binary `.res` and `.scn` are not decoded. |
| Local and cross-script calls | Partial | `test_same_script_calls_resolve_with_scope_and_shadowing_guards`, `test_typed_cross_script_calls_resolve_only_from_declared_receiver_types`, `test_receiver_reassignment_updates_or_invalidates_inferred_type`, `test_nested_class_types_nested_collections_and_static_factory_returns_resolve`, `test_literal_loaded_resource_uses_attached_script_for_calls_and_properties`, `test_known_receiver_without_project_method_reports_unresolved_target` | Resolves same-script calls, explicit `self`, `super`, unique project class receivers, autoload receivers, typed parameters/properties/locals, supported straight-line `Class.new()` reassignment, literal resource receivers, nested class-qualified types, nested `Array` / `Dictionary` element types, static factory return types, preload aliases, and calls owned by lambdas. Branch-sensitive reassignment invalidates inferred types with a diagnostic. Missing project method candidates are reported without inventing edges; Godot/external base APIs may be outside the index. Ambiguous classes fail closed. |
| Property usage | Partial | `test_script_property_reads_and_writes_resolve_to_property_nodes`, `test_typed_cross_script_property_reads_and_writes_resolve_to_definitions`, `test_tres_assignments_and_property_accessors_link_to_script_definitions`, `test_literal_property_get_set_and_inline_accessor_bodies_resolve`, `test_literal_loaded_resource_uses_attached_script_for_calls_and_properties` | Distinguishes script property reads/writes, typed cross-script and literal loaded-resource receivers, inline accessor body references, literal `get("name")` / `set("name", value)`, and serialized assignments where the script attachment and property are explicit. Computed property names remain dynamic and diagnosed. |
| Script signals | Partial | `test_script_signal_emits_and_literal_callbacks_resolve`, `test_inherited_script_signals_resolve_without_name_only_matches`, `test_lambda_calls_and_signal_callbacks_have_lambda_owners`, `test_signal_callbacks_resolve_only_from_typed_callable_receivers` | Records declared signal, emit, and `.connect` facts separately. Resolves direct local callback names, inline lambda callbacks, narrow `Callable(self, "method")`, and `Callable(typed_script_receiver, "method")` forms, including inherited signal lookup. Unknown or ambiguous Callable receivers and computed callbacks remain unresolved with diagnostics. |
| Project settings | Partial | `test_project_settings_resolve_main_scene_and_autoload_calls`, `test_project_enabled_plugin_links_to_indexed_plugin_cfg`, `test_nested_projects_keep_identical_res_paths_separate`, `test_project_config_header_and_semicolon_comments_are_valid` | Reads standard `config_version` headers, semicolon comments, main scene, autoloads, and enabled plugin paths from `project.godot`. Main scenes, autoload scripts/scenes, and existing plugin.cfg targets resolve within project identity. Plugin script execution and plugin-provided runtime registrations are not modeled. |
| Scene/resource files | Partial | `test_resource_parser_keeps_nested_links_and_never_scans_strings`, `test_gdscript_node_paths_unique_names_and_attached_receiver_calls`, `test_inherited_scene_nodes_and_attached_scripts_resolve_for_node_paths`, `test_nested_scene_instances_resolve_node_paths_and_attached_script_calls`, `test_inherited_node_override_replaces_base_script_attachment`, `test_nested_scene_instance_cycles_are_reported_without_recursion`, `test_deep_scene_instance_chain_stops_at_documented_limit` | Reads text `.tres` / `.tscn`, external and subresources, explicit script attachments, nested scene instances, root inherited scenes and node overrides, serialized signals, literal `$Path`, `%UniqueName`, and `get_node()` paths. Instanced node paths retain the host-scene context; cycles are diagnosed and expansion stops at 64 nested scenes. Absolute NodePaths, binary scenes/resources and all Godot override edge cases remain outside the static model. |
| Dynamic boundary evidence | Partial | `test_dynamic_godot_boundaries_report_reasons_without_guessed_edges` | Reports common computed loads, unresolved `call`/`call_deferred`, group lookups/dispatch, `set_script`, `add_child`, and dynamic signal callbacks. Literal `call()` targets resolve only with a unique supported receiver. This is a bounded inventory, not a complete model of reflection or runtime injection. |
| Source-group HTML filters | Implemented across graph, tree, and callflow HTML | `tests/test_export.py::test_html_source_groups_classify_paths_and_explicit_overrides`, `test_html_source_and_community_filters_intersect_at_runtime`, `test_html_aggregated_communities_retain_source_group_membership`, `test_html_aggregates_recompute_group_member_and_link_counts`, `tests/test_tree_html.py::test_build_tree_tracks_source_groups_and_grouped_truncation`, `test_tree_filtering_recomputes_visible_symbol_totals`, `tests/test_callflow_html.py::test_callflow_source_group_filters_annotate_rows_and_relationships`, `test_callflow_call_table_keeps_a_bounded_sample_per_source_group`, `test_callflow_source_group_filter_runtime_hides_excluded_rows_and_refs` | Docs, Tests, Scripts, Scenes, Resources, Tools, Addons, Config, and Other use one path/override classifier in all three HTML views. Graph filters intersect with community visibility, search, edge contributions, selection, and URL state. Oversized graph summaries retain per-group node and cross-community edge counts and recompute visible totals; communities remain aggregate nodes without individual-member drilldown. Tree filters symbols, recomputes directory counts and URL state, and attributes per-file truncation summaries to groups. Callflow filters rows and neighbor references, persists group state, reports visible versus included row counts, and hides full-graph diagrams/cards while any group is disabled. Its call table includes at most 30 rows per group; static summary diagrams return when all groups are restored. |

## Source-group rules

Every exported node receives one source group. Nodes inherit the group of their
source file where available. Addon and tool ownership takes precedence over
test ownership, which takes precedence over file-extension classification; an
explicit node `source_group` wins. `tests/` scripts therefore appear under
Tests, while tests under Addons remain under Addons.

At HTML export time, set `GRAPHIFY_SOURCE_GROUP_OVERRIDES` to a JSON object
whose keys are path globs and values are group IDs. For example:

```powershell
$env:GRAPHIFY_SOURCE_GROUP_OVERRIDES = '{"vendor/**":"other","tests/keep/**":"scripts"}'
graphify cluster-only .
Remove-Item Env:GRAPHIFY_SOURCE_GROUP_OVERRIDES
```

The accepted group IDs are `docs`, `tests`, `scripts`, `scenes`, `resources`,
`tools`, `addons`, `config`, and `other`. Path separators are normalized for
matching. The CLI help also documents this variable.

## Verification status

The Windows development environment currently has Tree-sitter 0.25.2 and the
`tree-sitter-gdscript` 6.1.0 grammar installed. The changed-area suite passed
98 tests (9 cache-root-switch warnings from the shared Windows test process).
The full fork suite, run before the final typed-property and cache-schema
changes, completed with 6,760 passed, 409 skipped, and 60 failed. The failures
were in shell-dependent hooks (`sh` is unavailable in this Windows shell),
optional language/dependency coverage, and Windows path-sensitive tests,
outside the changed-area suite. Linux clean-install coverage for the Godot extra,
a full cross-platform release gate,
broader parser corpus acceptance, and reviewed representative Tiny Demons query
answers remain release proof work. Rerun the focused tests with:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_gdscript.py tests/test_godot_text.py tests/test_export.py -q
```

The 2026-10-11 Tiny Demons corpus sweep parsed all 605 `.gd` files with 0
grammar-recovery diagnostics. This closes the seven known recovery spans by
normalizing compact compound statements while restoring original source lines.
It does not establish full GDScript grammar support; lambda and other uncovered
syntax still require dedicated fixtures. The sweep is integration evidence, not
a substitute for the grammar fixture suite.

After changing Graphify code, refresh its own graph with `graphify update .`.
For project integration, refresh the local Tiny Demons map separately; its
generated outputs are not source files and should remain ignored.

## Version policy

The fork is on `v8` and currently retains upstream package version `0.9.86`.
Do not bump Tiny Demons' game version for Graphify-only tooling changes. Bump
the fork package version and lockfile when a Graphify for Godot release is cut,
after the R1 clean-install and platform gates pass. This implementation slice
does not publish a package.

## Not currently supported

- Godot 3 compatibility, binary resource/scene decoding, C# interop, engine
  execution tracing, and runtime scene inspection.
- Dynamic or computed resource/NodePaths except for bounded diagnostics.
- A complete callable/member/call graph for reflection, arbitrary signal
  callbacks, group membership, runtime injection, or runtime `set_script()`.
- Flattening ordinary instanced scenes and all inherited-scene overrides.
- Source-group filters in Graphify's inventory tree and callflow HTML exports.
- Treating a missing static read edge as proof that an authored property is
  dead or unused at runtime.
