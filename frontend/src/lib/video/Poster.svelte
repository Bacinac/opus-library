<script lang="ts">
	// One film on a wall of films: the poster, its name, its year, and the mark
	// of its state when it is not simply on the shelf.

	import { type TagTone } from '$lib/kit';
	import { StateMark } from '$lib/opus';

	let {
		title,
		poster,
		sub = '',
		mark = null,
		markText = '',
		onclick
	}: {
		title: string;
		poster: string | null;
		sub?: string;
		mark?: { icon: string; tone: TagTone } | null;
		markText?: string;
		onclick: () => void;
	} = $props();
</script>

<button class="card" {onclick}>
	{#if poster}
		<img src={poster} alt={title} loading="lazy" />
	{:else}
		<div class="blank"></div>
	{/if}
	{#if mark}<span class="mark"><StateMark {...mark} text={markText} /></span>{/if}
	<span class="body">
		<strong>{title}</strong>
		{#if sub}<span class="sub">{sub}</span>{/if}
	</span>
</button>

<style>
	.card {
		position: relative;
		display: flex;
		flex-direction: column;
		padding: 0;
		overflow: hidden;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: var(--surface);
		color: inherit;
		font: inherit;
		text-align: left;
		cursor: pointer;
	}
	.card:hover {
		border-color: var(--accent);
		background: var(--surface-2);
	}
	img,
	.blank {
		display: block;
		width: 100%;
		aspect-ratio: 2 / 3;
		object-fit: cover;
		background: var(--surface-2);
	}
	.mark {
		position: absolute;
		top: 0.45rem;
		right: 0.45rem;
		display: flex;
		padding: 2px;
		border-radius: 999px;
		background: var(--veil);
	}
	.body {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		padding: 0.6rem 0.7rem 0.75rem;
	}
	strong {
		font-size: var(--fs-m);
		line-height: 1.25;
	}
	.sub {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
		color: var(--muted);
		font-size: var(--fs-m);
	}
</style>
