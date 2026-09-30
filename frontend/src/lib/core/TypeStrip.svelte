<script lang="ts">
	// The one control that switches which half of the library you are looking at.
	//
	// It does not change what a page shows — it changes which page you are on.
	// A wall of artists and a grid of posters are not one view with different
	// pictures, and pretending they are is how a merge ruins both halves.

	import { Tabs } from '$lib/kit';
	import { t } from '$lib/i18n';
	import { install } from './install.svelte';
	import type { MediaType } from './types';

	let {
		active = $bindable(),
		only
	}: {
		active: MediaType | null;
		/** narrow the strip to the types this page can actually answer for —
		 * web video has no catalogue to search, so Search does not offer it */
		only?: MediaType[];
	} = $props();

	const shown = $derived(
		only ? install.types.filter((type) => only.includes(type)) : install.types
	);

	$effect(() => {
		install.load();
	});

	$effect(() => {
		if (install.loaded && (active === null || !shown.includes(active))) {
			const remembered = install.remembered();
			active = remembered && shown.includes(remembered) ? remembered : (shown[0] ?? null);
		}
	});

	function pick(key: string) {
		active = key as MediaType;
		install.remember(active);
	}

	const tabs = $derived(shown.map((type) => ({ key: type, label: t(`type.${type}`) })));
</script>

{#if tabs.length > 1}
	<div class="strip">
		<Tabs {tabs} {active} onpick={pick} />
	</div>
{/if}

<style>
	.strip {
		margin-bottom: 1.25rem;
		border-bottom: 1px solid var(--border);
	}
</style>
