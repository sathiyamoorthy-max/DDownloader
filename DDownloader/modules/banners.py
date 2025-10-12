import os, time
from sys import stdout
from colorama import Fore, Style

# =========================================================================================================== #

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

# =========================================================================================================== #

def banners():
    stdout.write("                                                                                         \n")
    stdout.write(""+Fore.LIGHTRED_EX +"██████╗ ██████╗  ██████╗ ██╗    ██╗███╗   ██╗██╗      ██████╗  █████╗ ██████╗ ███████╗██████╗ \n")
    stdout.write(""+Fore.LIGHTRED_EX +"██╔══██╗██╔══██╗██╔═══██╗██║    ██║████╗  ██║██║     ██╔═══██╗██╔══██╗██╔══██╗██╔════╝██╔══██╗\n")
    stdout.write(""+Fore.LIGHTRED_EX +"██║  ██║██║  ██║██║   ██║██║ █╗ ██║██╔██╗ ██║██║     ██║   ██║███████║██║  ██║█████╗  ██████╔╝\n")
    stdout.write(""+Fore.LIGHTRED_EX +"██║  ██║██║  ██║██║   ██║██║███╗██║██║╚██╗██║██║     ██║   ██║██╔══██║██║  ██║██╔══╝  ██╔══██╗\n")
    stdout.write(""+Fore.LIGHTRED_EX +"██████╔╝██████╔╝╚██████╔╝╚███╔███╔╝██║ ╚████║███████╗╚██████╔╝██║  ██║██████╔╝███████╗██║  ██║\n")
    stdout.write(""+Fore.LIGHTRED_EX +"╚═════╝ ╚═════╝  ╚═════╝  ╚══╝╚══╝ ╚═╝  ╚═══╝╚══════╝ ╚═════╝ ╚═╝  ╚═╝╚═════╝ ╚══════╝╚═╝  ╚═╝\n")
    stdout.write(""+Fore.YELLOW +"═════════════╦═════════════════════════════════╦══════════════════════════════\n")
    stdout.write(""+Fore.YELLOW   +"╔════════════╩═════════════════════════════════╩═════════════════════════════╗\n")
    stdout.write(""+Fore.YELLOW   +"║ \x1b[38;2;255;20;147m• "+Fore.GREEN+"AUTHOR             "+Fore.RED+"    |"+Fore.LIGHTWHITE_EX+"   PARI MALAM                                    "+Fore.YELLOW+"║\n")
    stdout.write(""+Fore.YELLOW   +"╔════════════════════════════════════════════════════════════════════════════╝\n")
    stdout.write(""+Fore.YELLOW   +"║ \x1b[38;2;255;20;147m• "+Fore.GREEN+"GITHUB             "+Fore.RED+"    |"+Fore.LIGHTWHITE_EX+"   GITHUB.COM/THATNOTEASY                        "+Fore.YELLOW+"║\n")
    stdout.write(""+Fore.YELLOW   +"╚════════════════════════════════════════════════════════════════════════════╝\n") 
    print(f"{Fore.YELLOW}[DDownloader] - {Fore.GREEN}A DRM-Protected & Non-Protected Content Downloader - {Fore.RED}[V0.4.3] \n{Fore.RESET}")

# =========================================================================================================== #

def clear_and_print():
    time.sleep(1)
    clear_screen()
    banners()

# =========================================================================================================== #

def display_help():
    """Display custom help message with emoji."""
    print(
        f"{Fore.RED}.++" + "═" * 100 + f"++.{Style.RESET_ALL}\n"
        f"{Fore.CYAN}{'Option':<40}{'Description':<90}{Style.RESET_ALL}\n"
        f"{Fore.RED}.++" + "═" * 100 + f"++.{Style.RESET_ALL}\n"
        f"  {Fore.GREEN}-u, --url{' ' * 22}{Style.RESET_ALL}URL of the manifest (mpd/m3u8/ism) 🌐\n"
        f"  {Fore.GREEN}-p, --proxy{' ' * 20}{Style.RESET_ALL}A proxy with protocol (http://ip:port) 🌍\n"
        f"  {Fore.GREEN}-o, --output{' ' * 19}{Style.RESET_ALL}Name of the output file 💾\n"
        f"  {Fore.GREEN}-k, --key{' ' * 22}{Style.RESET_ALL}Decryption key in KID:KEY format 🔑\n"
        f"  {Fore.GREEN}-H, --header{' ' * 19}{Style.RESET_ALL}Custom HTTP headers (e.g., User-Agent: value) 📋\n"
        f"  {Fore.GREEN}-c, --cookies{' ' * 19}{Style.RESET_ALL}Cookies file (e.g., netscape/json formatter) 📋\n"
        f"{Fore.RED}.++" + "═" * 100 + f"++.{Style.RESET_ALL}\n"
        f"  {Fore.GREEN}-i, --input{' ' * 20}{Style.RESET_ALL}Input file for re-encoding. 📂\n"
        f"  {Fore.GREEN}-q, --quality{' ' * 18}{Style.RESET_ALL}Target quality: HD, FHD, UHD. 🎥\n"
        f"  {Fore.GREEN}-h, --help{' ' * 21}{Style.RESET_ALL}Show this help message and exit ❓\n"
        f"{Fore.RED}.++" + "═" * 100 + f"++.{Style.RESET_ALL}\n"
    )
    
# =========================================================================================================== #
