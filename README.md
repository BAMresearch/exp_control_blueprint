# new_exp_control_design

Collection of code and scripts to automate chemistry experiments. Can be used as is with minor adjustments but is supposed to be a blueprint/inspiration for your own projects.

The file "simulated_setup.py" can be used as reference for basic instructions and workflow. Here, simple topologies and experiments are implemented for testing purposes.

If you want to write your own experiment file, make a copy of "simulated_setup.py" and adjust device imports, topologies and recipes to your needs.

Keep in mind, that this repository is written specifically for using the following hardware:
Magnetic stirrers/Hotplates from IKA
Peristaltic pumps from Longer/Drifton
Syringe pumps from Aladdin WPI
Multi-way-valves from Vici

If you are using other hardware, you will need to provide your own python files and commands about how to connect and control them. This then needs to be implemented in "basic_commands.py" and possibly other files as well.