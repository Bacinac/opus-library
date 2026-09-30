<script lang="ts">
	// What a guest sees here, which is a sentence and a way out.
	//
	// This module is where the library is maintained and where the family album
	// lives. A guest is trusted with the address and not handed the album, so
	// neither half is theirs — and the films and the records they ARE here for
	// are watched in the Player, not in this catalogue.

	import { Button, Card } from '$lib/kit';
	import { t } from '$lib/i18n';
	import { MODULES } from '$lib/core/modules';

	const player = MODULES.find((m) => m.key === 'player');
</script>

<div class="away">
	<Card title={t('guest.title')}>
		<p>{t('guest.says')}</p>
		{#if player}
			<div><Button tone="primary" href={player.href}>{t('guest.toPlayer')}</Button></div>
		{/if}
	</Card>
</div>

<style>
	.away {
		max-width: 34rem;
		margin: 3rem auto;
	}
	p {
		margin: 0;
		color: var(--muted);
	}
</style>
