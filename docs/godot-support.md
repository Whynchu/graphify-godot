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
| Script structure | Partial | `test_script_class_methods_and_exported_properties_are_extracted`, `test_signals_enums_constants_and_nested_members_are_owned` | Covers script classes, methods, top-level properties, signals, enums, constants, nested classes, static methods, annotations, parameter/return text, and accessor metadata. Lambdas, all grammar syntax, and precise recovery spans remain open. A semicolon normalization handles the known Tiny Demons grammar gap while restoring source locations. |
| Literal resource references | Implemented | `test_literal_resource_and_global_class_references_are_extracted`, `test_gdscript_smoke_dependencies_resolve_to_resources_and_global_classes` | Resolves literal `preload`, `load`, and `ResourceLoader.load` paths in the indexed project. Computed paths are reported as dynamic. Binary `.res` and `.scn` are not decoded. |
| Local and cross-script calls | Partial | `test_same_script_calls_resolve_with_scope_and_shadowing_guards`, `test_typed_cross_script_calls_resolve_only_from_declared_receiver_types` | Resolves same-script calls, explicit `self`, `super`, unique project class receivers, autoload receivers, typed parameters/properties/locals, supported `Class.new()` inference, preload aliases, and indexed elements of `Array[Type]` / `Dictionary[Key, Type]`. Return-value inference, nested generic types, branch-sensitive reassignment, and all nested-class type spellings are not modeled. Ambiguous classes fail closed. |
| Property usage | Partial | `test_script_property_reads_and_writes_resolve_to_property_nodes`, `test_typed_cross_script_property_reads_and_writes_resolve_to_definitions`, `test_tres_assignments_and_property_accessors_link_to_script_definitions` | Distinguishes script property reads/writes, typed cross-script receivers, named accessor links, and serialized assignments where the script attachment and property are explicit. Inline accessors are metadata. Dynamic property access and receiver inference from an untyped `.tres` load are incomplete. |
| Script signals | Partial | `test_script_signal_emits_and_literal_callbacks_resolve`, `test_inherited_script_signals_resolve_without_name_only_matches` | Records declared signal, emit, and `.connect` facts separately. Resolves direct local callback names and narrow `Callable(self, "method")` forms, including inherited signal lookup. Arbitrary Callable receivers and computed callbacks remain unresolved with diagnostics. |
| Project settings | Partial | `test_project_settings_resolve_main_scene_and_autoload_calls`, `test_nested_projects_keep_identical_res_paths_separate` | Reads the main scene, autoloads, and enabled plugin paths from `project.godot`. Main scenes and autoload scripts/scenes resolve within project identity. Plugin target files such as `plugin.cfg` are not yet modeled as Godot resources. |
| Scene/resource files | Partial | `test_resource_parser_keeps_nested_links_and_never_scans_strings`, `test_gdscript_node_paths_unique_names_and_attached_receiver_calls` | Reads text `.tres` / `.tscn`, external and subresources, explicit script attachments, scene instances, serialized signals, literal `$Path`, `%UniqueName`, and `get_node()` paths. Root inherited-scene trees and their explicit attached scripts are followed. Ordinary nested scene instances remain opaque; inherited overrides and all Godot `NodePath` edge cases are not flattened. |
| Dynamic boundary evidence | Partial | `test_dynamic_godot_boundaries_report_reasons_without_guessed_edges` | Reports common computed loads, unresolved `call`/`call_deferred`, group lookups/dispatch, `set_script`, `add_child`, and dynamic signal callbacks. Literal `call()` targets resolve only with a unique supported receiver. This is a bounded inventory, not a complete model of reflection or runtime injection. |
| Source-group HTML filters | Implemented for main HTML | `tests/test_export.py::test_html_source_groups_classify_paths_and_explicit_overrides`, `test_html_source_and_community_filters_intersect_at_runtime`, `test_html_aggregated_communities_retain_source_group_membership` | Docs, Tests, Scripts, Scenes, Resources, Tools, Addons, Config, and Other can be filtered in the exported `graph.html`; source and community filters intersect, affect search/edges/selection, and persist in the URL fragment. Filters only affect exported nodes. Aggregated communities cannot reveal their member nodes. Tree and callflow HTML controls are deferred. |

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

The current Tiny Demons corpus sweep found 605 `.gd` files, 0 extractor
crashes, 1,138 semicolon statement splits, and 7 remaining grammar recovery
files: `scripts/components/slime_visual_component.gd`,
`scripts/runtime/controllers/gameplay.gd`,
`scripts/runtime/controllers/room_controller.gd`,
`scripts/runtime/state/gameplay_state.gd`, `scripts/ui/cloud_save_panel.gd`,
`scripts/ui/hub_input_controller.gd`, and
`scripts/ui/screen_route_controller.gd`. These files may have partial graph
data; inspect their source spans before relying on missing edges.

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
