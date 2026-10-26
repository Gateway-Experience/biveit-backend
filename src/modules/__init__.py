# Standalone modules -- features enclosed enough to stand on their own, each exposing its interface through its
# own __init__ (callers import the module, never its files). Inside, a module can have its own services/ and
# repositories/ (and the rest of the app's layering); its controllers, if any, stay in src/controllers.
# Promoting something to a module is a deliberate call, not a default home for new code.
