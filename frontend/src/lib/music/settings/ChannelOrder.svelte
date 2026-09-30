<script lang="ts">
	// The download channels in the order they get tried, reordered by dragging
	// or, where there is no pointer to drag with, a step at a time.
	// The stored value is a comma list read defensively — an unknown or repeated
	// name is dropped and a missing one appended — so the list always shows
	// every channel exactly once whatever is in the setting.

	import { t, type MessageKey } from '$lib/i18n';
	import { Button } from '$lib/kit';

	let { value = $bindable(), channels }: { value: string; channels: string[] } = $props();

	let dragIndex = $state<number | null>(null);
	let dropIndex = $state<number | null>(null);

	let channelOrder = $derived.by(() => {
		const seen = new Set<string>();
		const order = (value ?? '')
			.split(',')
			.map((part) => part.trim())
			.filter((part) => channels.includes(part) && !seen.has(part) && (seen.add(part), true));
		for (const channel of channels) if (!seen.has(channel)) order.push(channel);
		return order;
	});

	function move(from: number, to: number) {
		if (to < 0 || to >= channelOrder.length) return;
		const next = [...channelOrder];
		const [moved] = next.splice(from, 1);
		next.splice(to, 0, moved);
		value = next.join(',');
	}

	function dragStart(event: DragEvent, index: number) {
		dragIndex = index;
		if (event.dataTransfer) {
			event.dataTransfer.effectAllowed = 'move';
			event.dataTransfer.setData('text/plain', String(index));
		}
	}

	function dragOver(event: DragEvent, index: number) {
		event.preventDefault();
		if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
		dropIndex = index;
	}

	function drop(event: DragEvent, index: number) {
		event.preventDefault();
		if (dragIndex !== null && dragIndex !== index) move(dragIndex, index);
		dragIndex = null;
		dropIndex = null;
	}

	function dragEnd() {
		dragIndex = null;
		dropIndex = null;
	}
</script>

<div class="row">
	<span class="label">{t('field.channel_order')}</span>
	<div class="channel-list" role="list">
		{#each channelOrder as channel, i (channel)}
			<div
				role="listitem"
				class="channel-row"
				class:dragging={dragIndex === i}
				class:drop-target={dropIndex === i && dragIndex !== null && dragIndex !== i}
				draggable="true"
				ondragstart={(e) => dragStart(e, i)}
				ondragover={(e) => dragOver(e, i)}
				ondrop={(e) => drop(e, i)}
				ondragend={dragEnd}
			>
				<span class="handle" aria-hidden="true">⠿</span>
				<span class="pos">{i + 1}</span>
				<span class="name">{t(`service.${channel}` as MessageKey)}</span>
				<span class="steps">
					<Button size="small" label={t('settings.moveUp')} disabled={i === 0} onclick={() => move(i, i - 1)}>↑</Button>
					<Button size="small" label={t('settings.moveDown')} disabled={i === channelOrder.length - 1}
						onclick={() => move(i, i + 1)}>↓</Button>
				</span>
			</div>
		{/each}
	</div>
</div>

<style>
	.row {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
		min-width: 0;
	}
	.label {
		font-weight: 600;
		font-size: var(--fs-m);
	}
	.channel-list {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
	.channel-row {
		display: flex;
		align-items: center;
		gap: 0.6rem;
		padding: 0.45rem 0.75rem;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--surface-2);
		cursor: grab;
		user-select: none;
	}
	.channel-row:active {
		cursor: grabbing;
	}
	.channel-row.dragging {
		opacity: 0.45;
	}
	.channel-row.drop-target {
		border-color: var(--accent);
		box-shadow: inset 0 0 0 1px var(--accent);
	}
	.handle {
		color: var(--muted);
		font-size: var(--fs-l);
		line-height: 1;
	}
	.pos {
		color: var(--muted);
		font-size: var(--fs-s);
		font-variant-numeric: tabular-nums;
		min-width: 1.1em;
		text-align: center;
	}
	.name {
		font-weight: 600;
		font-size: var(--fs-l);
	}
	.steps {
		display: flex;
		gap: 0.25rem;
		margin-left: auto;
	}
</style>
