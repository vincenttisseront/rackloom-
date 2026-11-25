<script setup lang="ts">
defineProps<{ inbox: { mine: any[]; waiting: any[]; notice?: string } | null }>()
</script>

<template>
  <div v-if="inbox" class="inbox" data-testid="inbox">
    <section aria-labelledby="inbox-mine-title" data-testid="inbox-mine">
      <h2 id="inbox-mine-title">À moi d'agir</h2>
      <p class="small muted">{{ inbox.notice }}</p>
      <ul v-if="inbox.mine.length" class="inbox-list">
        <li v-for="item in inbox.mine" :key="item.change_id" class="inbox-item" data-testid="inbox-mine-item">
          <div class="field-head">
            <NuxtLink :to="`/demandes/${item.change_id}`" data-testid="inbox-open">
              {{ item.ref }} · {{ item.title }}
            </NuxtLink>
            <StatePill :state="item.state" :label="item.state_label" />
          </div>
          <p class="small" data-testid="inbox-blocker">{{ item.blocker }}</p>
          <p class="small muted">
            Qui peut agir : {{ item.who_can_act.join(', ') || '—' }}
          </p>
          <p v-if="item.next_action" class="small">
            Action suivante :
            <NuxtLink
              class="button primary"
              :to="`/demandes/${item.change_id}`"
              data-testid="inbox-next-action"
            >
              {{ item.next_action.label }}
            </NuxtLink>
          </p>
        </li>
      </ul>
      <p v-else class="empty small" data-testid="inbox-mine-empty">Rien pour vous pour le moment.</p>
    </section>

    <section aria-labelledby="inbox-waiting-title" data-testid="inbox-waiting" style="margin-top: 1.25rem">
      <h2 id="inbox-waiting-title">En attente d'une autre personne</h2>
      <ul v-if="inbox.waiting.length" class="inbox-list">
        <li v-for="item in inbox.waiting" :key="item.change_id" class="inbox-item" data-testid="inbox-waiting-item">
          <div class="field-head">
            <NuxtLink :to="`/demandes/${item.change_id}`" data-testid="inbox-waiting-open">
              {{ item.ref }} · {{ item.title }}
            </NuxtLink>
            <StatePill :state="item.state" :label="item.state_label" />
          </div>
          <p class="small" data-testid="inbox-waiting-blocker">{{ item.blocker }}</p>
          <p class="small muted">
            Qui peut agir : {{ item.who_can_act.join(', ') || '—' }}
          </p>
        </li>
      </ul>
      <p v-else class="empty small" data-testid="inbox-waiting-empty">Aucune attente visible.</p>
    </section>
  </div>
</template>
