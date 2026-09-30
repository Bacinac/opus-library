<script lang="ts">
	// A folded table of folders under the shelf: what the last scan made of each
	// one, or which ones it could not place. One look for both, because they are
	// the same kind of list read the same way.

	import type { Snippet } from 'svelte';

	let {
		summary,
		open = false,
		above,
		children
	}: { summary: string; open?: boolean; above?: Snippet; children: Snippet } = $props();
</script>

<details class="report" {open}>
	<summary>{summary}</summary>
	{#if above}{@render above()}{/if}
	<table>
		<tbody>
			{@render children()}
		</tbody>
	</table>
</details>

<style>
	.report {
		margin-top: 2.5rem;
		border-top: 1px solid var(--border);
		padding-top: 1rem;
	}
	summary {
		cursor: pointer;
		color: var(--muted);
		font-size: var(--fs-m);
	}
	table {
		margin-top: 0.5rem;
		font-size: var(--fs-m);
	}
	.report :global(td) {
		padding: 0.35rem 0.6rem;
		vertical-align: top;
		white-space: normal;
	}
	.report :global(td.folder) {
		max-width: 320px;
		overflow-wrap: anywhere;
	}
	.report :global(td.folder code) {
		font-size: 0.85em;
	}
	.report :global(.failed) {
		color: var(--danger);
	}
	.report :global(.partial) {
		color: var(--warn);
	}
</style>
