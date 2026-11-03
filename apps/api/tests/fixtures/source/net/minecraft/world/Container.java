package net.minecraft.world;

import java.util.List;

public class Container<T extends Entity> {
    private final List<T> items;

    public Container(List<T> items) {
        this.items = items;
    }

    public T get(int index) {
        return items.get(index);
    }

    public class Slot {
        public int index;

        public T owner() {
            return items.get(index);
        }
    }
}
