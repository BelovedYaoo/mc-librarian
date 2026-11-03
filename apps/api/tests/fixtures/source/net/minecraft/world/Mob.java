package net.minecraft.world;

@Deprecated
public abstract class Mob extends Entity implements Trackable {
    @Nullable
    protected String name;
    protected int health = 20;

    @Override
    public void track() {
        tick();
        this.health--;
    }

    public String getName() {
        return name;
    }
}
