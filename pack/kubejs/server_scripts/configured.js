
let expandedMobSpawning = true
let mediumHideBeds = false

const config = JsonIO.read('config/atfc/config.json')
if (config != null){
    expandedMobSpawning = config.expandedMobSpawning
    mediumHideBeds = config.mediumHideBeds
}

onEvent('tags.blocks', event => 
{
    if (expandedMobSpawning){
        event.add('tfc:monster_spawns_on', ['#minecraft:mineable/axe', '#minecraft:mineable/pickaxe', '#minecraft:mineable/shovel', 'minecraft:tinted_glass', 'minecraft:bedrock'])
    }
})

onEvent('tags.items', event => 
{
    if (mediumHideBeds){
        event.add('tfc:thatch_bed_hides', ['tfc:medium_raw_hide', 'tfc:medium_sheepskin_hide'])
    }
})
