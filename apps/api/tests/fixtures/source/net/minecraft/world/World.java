package net.minecraft.world;

public class World {
    public int time;

    public void spawn(Mob mob, Container<Entity> container) {
        mob.tick();
        mob.health = 10;
        String label = mob.getName();
        container.get(0);
        time++;
    }
}
