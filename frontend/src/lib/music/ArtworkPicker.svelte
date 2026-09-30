<script lang="ts">
	import { t } from '$lib/i18n';
	import { Button, Dialog, formatNumber, request, toasts } from '$lib/kit';
	import type { ArtworkImage } from '$lib/music/types';

	let {
		entity,
		entityId,
		title,
		onclose,
		onchanged
	}: {
		entity: 'artists' | 'releases';
		entityId: number;
		title: string;
		onclose: () => void;
		onchanged: () => void;
	} = $props();

	let images = $state<ArtworkImage[]>([]);
	let loaded = $state(false);
	let refreshing = $state(false);

	const base = $derived(`/api/music/${entity}/${entityId}/artwork`);

	async function load() {
		const got = await request<ArtworkImage[]>(base);
		if (!got) return;
		images = got;
		loaded = true;
	}

	$effect(() => {
		load();
	});

	async function refresh() {
		refreshing = true;
		const got = await request<ArtworkImage[]>(`${base}/refresh`, { method: 'POST' });
		refreshing = false;
		if (!got) return;
		images = got;
		onchanged();
	}

	async function choose(image: ArtworkImage) {
		if (!(await request(`${base}/${image.id}`, { method: 'PUT' }))) return;
		toasts.success(t('artwork.chosen'));
		await load();
		onchanged();
	}
</script>

<Dialog {title} {onclose}>
	{#snippet actions()}
		<Button onclick={refresh} disabled={refreshing}>
			{refreshing ? t('artwork.refreshing') : t('artwork.refresh')}
		</Button>
	{/snippet}
	{#if !loaded}
		<p class="muted">{t('common.loading')}</p>
	{:else if images.length === 0}
		<p class="muted">{t('artwork.empty')}</p>
	{:else}
		<div class="grid">
			{#each images as image (image.id)}
				<button class="candidate" class:chosen={image.chosen} onclick={() => choose(image)}>
					<img src={image.url} alt={image.source} loading="lazy" />
					<span class="caption">
						{image.source}
						{#if image.width}· {formatNumber(image.width)}×{formatNumber(image.height ?? 0)}{/if}
						{#if image.chosen_manual}· {t('artwork.manual')}{/if}
					</span>
				</button>
			{/each}
		</div>
	{/if}
</Dialog>

<style>
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
		gap: 0.75rem;
	}
	.candidate {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
		padding: 0.5rem;
		background: var(--surface);
		border: 2px solid var(--border);
		border-radius: 10px;
		cursor: pointer;
		font: inherit;
		color: inherit;
	}
	.candidate:hover {
		border-color: var(--accent);
	}
	.candidate.chosen {
		border-color: var(--ok);
	}
	.candidate img {
		width: 100%;
		aspect-ratio: 1;
		object-fit: cover;
		border-radius: 6px;
		background: var(--surface-2);
	}
	.caption {
		font-size: var(--fs-s);
		color: var(--muted);
	}
</style>
