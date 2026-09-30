<script lang="ts" generics="T">
	import type { Snippet } from 'svelte';
	import { t } from '$lib/i18n';
	import { Button } from '$lib/kit';

	let {
		items,
		key,
		picture,
		poster = false,
		info,
		onadd
	}: {
		items: T[];
		key: (item: T) => unknown;
		picture: (item: T) => { src: string | null; alt: string };
		poster?: boolean;
		info: Snippet<[T]>;
		onadd: (item: T) => void;
	} = $props();
</script>

<ul>
	{#each items as item (key(item))}
		{@const shown = picture(item)}
		<li>
			{#if shown.src}
				<img class:poster src={shown.src} alt={shown.alt} loading="lazy" />
			{:else}
				<div class="blank" class:poster></div>
			{/if}
			<div class="info">{@render info(item)}</div>
			<Button tone="accent" onclick={() => onadd(item)}>{t('action.add')}</Button>
		</li>
	{/each}
</ul>

<style>
	ul {
		list-style: none;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
	}
	li {
		display: flex;
		align-items: center;
		gap: 1rem;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 10px;
		padding: 0.6rem 1rem;
	}
	img,
	.blank {
		flex: none;
		width: 56px;
		height: 56px;
		border-radius: 8px;
		object-fit: cover;
		background: var(--surface-2);
	}
	.poster {
		height: 84px;
	}
	.info {
		display: flex;
		flex-direction: column;
		flex: 1;
		min-width: 0;
		font-size: var(--fs-m);
	}
</style>
