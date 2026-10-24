<script lang="ts">
	import { untrack } from 'svelte';
	import { t } from '#lib/i18n/index.js';
	import { Chips, Dialog, SaveButton, json, request, toasts } from '#lib/kit/index.js';
	import type { ArtistAlias } from '#lib/music/types.js';

	let {
		artistId,
		name,
		aliases,
		onclose,
		onsaved
	}: {
		artistId: number;
		name: string;
		aliases: ArtistAlias[];
		onclose: () => void;
		onsaved: () => void;
	} = $props();

	const listed = $derived(aliases.filter((a) => a.source === 'wikidata').map((a) => a.alias));
	const stored = $derived(aliases.filter((a) => a.source === 'manual').map((a) => a.alias));
	let own = $state(untrack(() => [...stored]));
	let saving = $state(false);
	const dirty = $derived(own.join('\n') !== stored.join('\n'));

	async function save() {
		saving = true;
		const saved = await request<ArtistAlias[]>(
			`/api/music/artists/${artistId}/aliases`,
			json({ aliases: own }, 'PUT')
		);
		saving = false;
		if (!saved) return;
		toasts.success(t('artist.aliasesSaved'));
		onsaved();
		onclose();
	}
</script>

<Dialog title={t('artist.aliases')} subtitle={name} size="narrow" {onclose}>
	<p class="note">{t('artist.aliasesNote')}</p>
	{#if listed.length > 0}
		<div class="listed">
			<span class="label">{t('artist.aliasesWikidata')}</span>
			<p>{listed.join(' · ')}</p>
		</div>
	{/if}
	<Chips
		id="artist-aliases"
		label={t('artist.aliasesOwn')}
		bind:values={own}
		placeholder={t('artist.aliasesPlaceholder')}
	/>
	<div class="actions">
		<SaveButton {dirty} {saving} onclick={save} />
	</div>
</Dialog>

<style>
	.note {
		margin: 0 0 1rem;
		color: var(--muted);
	}
	.listed {
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		margin-bottom: 1rem;
	}
	.listed p {
		margin: 0;
	}
	.label {
		font-size: var(--fs-s);
		color: var(--muted);
	}
	.actions {
		display: flex;
		justify-content: flex-end;
		margin-top: 1rem;
	}
</style>
